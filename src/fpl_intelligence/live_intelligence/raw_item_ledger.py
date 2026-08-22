"""Phase 9.2 — Raw Item Ledger and Extraction Bridge.

The single point where externally-captured text enters the Phase 9 pipeline.
This module is deliberately additive: it does not touch the quantitative
Phases 1–8 stack and it builds directly on the Phase 9.1 temporal ledger and
LLM extractor rather than re-implementing them.

What it guarantees
------------------

* :class:`RawItem` is a Pydantic model that *cannot* carry an inconsistent
  temporal footprint: ``available_at >= published_at``, ``published_at <=
  scraped_at`` and ``scraped_at <= ingested_at`` are validated on construction.
  Naive (timezone-less) datetimes are rejected outright.
* :class:`RawItemDeduplicator` ensures the same ``content_hash`` from the same
  ``source_id`` is processed at most once, backed by the Phase 9.1 ledger's
  unique ``(source_id, content_hash)`` constraint and fronted by an optional
  in-memory cache.
* :func:`ingest_raw_text` is the orchestration: hash -> dedup -> persist ->
  project into a :class:`LedgerItemView` -> extract with the Phase 9.1
  :class:`PromptedLLMExtractor` -> persist evidence through the existing
  :func:`persist_extraction`. The RawItem's ``source_id`` / ``published_at`` /
  ``available_at`` ride along into the Phase 7/8 evidence tables because the
  extractor inherits them from the ledger view, never from the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.models import AvailabilityEvidence
from fpl_intelligence.db.models import Gameweek, Season
from fpl_intelligence.features.temporal import InformationAccessPolicy
from fpl_intelligence.live_intelligence.entity_resolution import (
    build_entity_resolver as entity_resolution_build_entity_resolver,
)
from fpl_intelligence.live_intelligence.extraction import (
    LLMProvider,
    PersistenceReport,
    PromptedLLMExtractor,
    persist_extraction,
)
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider
from fpl_intelligence.live_intelligence.models import (
    LedgerTemporalClass,
    LiveAvailabilityEvidenceLink,
    LiveIntelligenceRawItem,
    TacticalEvidence,
    UnresolvedLiveEvidence,
)
from fpl_intelligence.live_intelligence.source_registry import (
    SourceRegistry,
    SourceType,
)
from fpl_intelligence.live_intelligence.temporal_ledger import (
    AvailabilityDerivationPolicy,
    Clock,
    LedgerItemView,
    TemporalLedger,
    build_timestamps,
    classify_ledger_entry,
    content_hash,
    utc_now,
)


class RawTemporalClass(StrEnum):
    """Temporal classification of a raw item, in Phase 9.2 vocabulary.

    ``post_match`` is distinct from ``post_deadline`` semantically (it
    describes content whose subject *event* has finished) but is treated as
    after-deadline for the purposes of decision eligibility, because a
    post-match report is never available before the gameweek deadline.
    """

    PRE_DEADLINE = "pre_deadline"
    POST_MATCH = "post_match"
    POST_DEADLINE = "post_deadline"
    NO_DEADLINE_CONTEXT = "no_deadline_context"


def map_temporal_class(raw_class: str) -> LedgerTemporalClass:
    """Project a :class:`RawTemporalClass` value onto the Phase 9.1 enum."""
    try:
        raw = RawTemporalClass(raw_class)
    except ValueError:
        return LedgerTemporalClass.NO_DEADLINE_CONTEXT
    return {
        RawTemporalClass.PRE_DEADLINE: LedgerTemporalClass.PRE_DEADLINE,
        RawTemporalClass.POST_MATCH: LedgerTemporalClass.POST_DEADLINE,
        RawTemporalClass.POST_DEADLINE: LedgerTemporalClass.POST_DEADLINE,
        RawTemporalClass.NO_DEADLINE_CONTEXT: LedgerTemporalClass.NO_DEADLINE_CONTEXT,
    }[raw]


# ---------------------------------------------------------------------------
# RawItem — the canonical ingested-content model
# ---------------------------------------------------------------------------


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware; got naive datetime {value!r}")


class RawItem(BaseModel):
    """Immutable representation of one ingested unit of raw text.

    Every temporal field is mandatory and validated for internal consistency.
    ``content_hash`` is the SHA-256 (whitespace-normalised) of ``content_text``
    and is what makes re-ingestion idempotent.
    """

    model_config = ConfigDict(extra="forbid")

    raw_item_id: int | None = None
    source_id: str
    external_id: str | None = None
    url: str | None = None
    title: str
    content_text: str
    content_hash: str
    published_at: datetime
    scraped_at: datetime
    available_at: datetime
    ingested_at: datetime
    temporal_class: str

    @model_validator(mode="after")
    def _validate_temporal(self) -> RawItem:
        _require_aware("published_at", self.published_at)
        _require_aware("scraped_at", self.scraped_at)
        _require_aware("available_at", self.available_at)
        _require_aware("ingested_at", self.ingested_at)

        if self.published_at > self.scraped_at:
            raise ValueError(
                f"published_at ({self.published_at.isoformat()}) is after "
                f"scraped_at ({self.scraped_at.isoformat()})"
            )
        if self.scraped_at > self.ingested_at:
            raise ValueError(
                f"scraped_at ({self.scraped_at.isoformat()}) is after "
                f"ingested_at ({self.ingested_at.isoformat()})"
            )
        if self.available_at < self.published_at:
            raise ValueError(
                f"available_at ({self.available_at.isoformat()}) precedes "
                f"published_at ({self.published_at.isoformat()})"
            )
        if self.available_at > self.ingested_at:
            raise ValueError(
                f"available_at ({self.available_at.isoformat()}) is after "
                f"ingested_at ({self.ingested_at.isoformat()})"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        title: str,
        content_text: str,
        published_at: datetime,
        scraped_at: datetime,
        ingested_at: datetime,
        url: str | None = None,
        external_id: str | None = None,
        available_at: datetime | None = None,
        temporal_class: str = RawTemporalClass.NO_DEADLINE_CONTEXT.value,
    ) -> RawItem:
        """Build a RawItem, computing the content hash and defaulting ``available_at``.

        ``available_at`` defaults to ``published_at`` when not supplied, which
        is the honest minimum: we never claim access before publication.
        """
        _require_aware("published_at", published_at)
        _require_aware("scraped_at", scraped_at)
        _require_aware("ingested_at", ingested_at)
        return cls(
            source_id=source_id,
            title=title,
            content_text=content_text,
            url=url,
            external_id=external_id,
            content_hash=content_hash(content_text),
            published_at=published_at,
            scraped_at=scraped_at,
            available_at=available_at or published_at,
            ingested_at=ingested_at,
            temporal_class=temporal_class,
        )


# ---------------------------------------------------------------------------
# Deduplication engine
# ---------------------------------------------------------------------------


class RawItemDeduplicator:
    """Ensure the same ``(source_id, content_hash)`` is processed once.

    The ground truth is the Phase 9.1 ledger's unique ``(source_id,
    content_hash)`` constraint. An optional in-memory cache fronts it so a tight
    loop that re-reads the same page does not issue a query per item.
    """

    def __init__(self, db: Session, *, use_cache: bool = True) -> None:
        self._db = db
        self._ledger = TemporalLedger(db)
        self._cache: set[tuple[int, str]] | None = set() if use_cache else None

    def is_duplicate(self, source_db_id: int, content_hash_value: str) -> bool:
        key = (source_db_id, content_hash_value)
        if self._cache is not None and key in self._cache:
            return True
        existing = self._ledger.find_by_hash(source_db_id, content_hash_value)
        if existing is not None:
            if self._cache is not None:
                self._cache.add(key)
            return True
        return False

    def remember(self, source_db_id: int, content_hash_value: str) -> None:
        if self._cache is not None:
            self._cache.add((source_db_id, content_hash_value))


# ---------------------------------------------------------------------------
# Raw Item Ledger
# ---------------------------------------------------------------------------


class RawItemLedger:
    """Persists :class:`RawItem` into the Phase 9.1 temporal ledger.

    Deduplication is enforced here, so a caller cannot accidentally double-write
    the same content for a source. On a duplicate the persist call returns
    ``None`` and the caller is expected to skip extraction.
    """

    def __init__(
        self,
        db: Session,
        *,
        clock: Clock = utc_now,
        policy: InformationAccessPolicy = InformationAccessPolicy.STRICT_REPRODUCIBILITY,
    ) -> None:
        self._db = db
        self._clock = clock
        self._policy = policy
        self._ledger = TemporalLedger(db, clock=clock, policy=policy)
        self._dedup = RawItemDeduplicator(db)

    @property
    def ledger(self) -> TemporalLedger:
        return self._ledger

    def is_duplicate(self, source_db_id: int, content_hash_value: str) -> bool:
        return self._dedup.is_duplicate(source_db_id, content_hash_value)

    def persist(
        self,
        raw: RawItem,
        *,
        source_db_id: int,
        content_type: str = "text",
        season_id: int | None = None,
        gameweek_id: int | None = None,
        deadline_at: datetime | None = None,
        external_id: str | None = None,
    ) -> LiveIntelligenceRawItem | None:
        """Persist a RawItem, or return ``None`` if it is a duplicate.

        ``external_id`` and the Phase 9.2 ``source_id`` are carried in
        ``metadata_json`` so provenance survives into the audit trail even
        though the underlying table has no dedicated columns for them.
        """
        if self._dedup.is_duplicate(source_db_id, raw.content_hash):
            return None

        temporal_class = map_temporal_class(raw.temporal_class)
        metadata = {
            "phase9_2_source_id": raw.source_id,
            "external_id": external_id if external_id is not None else raw.external_id,
        }
        item = LiveIntelligenceRawItem(
            source_id=source_db_id,
            content_hash=raw.content_hash,
            title=raw.title,
            raw_text=raw.content_text,
            url=raw.url,
            content_type=content_type,
            season_id=season_id,
            gameweek_id=gameweek_id,
            published_at=raw.published_at,
            scraped_at=raw.scraped_at,
            available_at=raw.available_at,
            ingested_at=raw.ingested_at,
            publication_established=True,
            deadline_at=deadline_at,
            temporal_class=temporal_class,
            access_policy=str(self._policy),
            metadata_json=json.dumps(metadata),
        )
        self._db.add(item)
        self._db.flush()
        self._db.refresh(item)
        self._dedup.remember(source_db_id, raw.content_hash)
        return item

    def to_ledger_view(self, item: LiveIntelligenceRawItem) -> LedgerItemView:
        """Project a persisted ledger row into the read-only extractor view."""
        return self._ledger.to_view(item)


# ---------------------------------------------------------------------------
# Extraction bridge helpers
# ---------------------------------------------------------------------------


def build_entity_resolver(db: Session):
    """Return a resolver usable for both players and teams by hint.

    Delegates to :func:`fpl_intelligence.live_intelligence.entity_resolution.build_entity_resolver`,
    which returns a :class:`ResolutionResult` (status + canonical id + reason)
    instead of a bare id. The resolution priority is external-id first, then
    name+team+season context, then unique name, surfacing ambiguity instead of
    guessing.

    The returned callable is ``(name, team=None, *, external_id=None,
    season_id=None) -> ResolutionResult``.
    """
    return entity_resolution_build_entity_resolver(db)


@dataclass(frozen=True)
class IngestedEvidenceSnapshot:
    """Detached, in-memory projection of one persisted evidence row.

    Captured *before* a dry-run rollback, so the Phase 9.3 AI Analyst can reason
    over exactly the evidence this extraction produced without keeping the
    transaction open and without leaving a permanent row behind. Every field is
    a primitive: the snapshot survives ``Session.rollback()`` and expiry because
    it holds no ORM identity.

    Temporal fields are inherited from the ledger row (the source of truth),
    never from the model, and ``is_mock`` is carried from the extraction run so
    scaffold artefacts can never be read as real evidence.
    """

    evidence_ref: str
    kind: str  # "availability" | "tactical"
    subject_ref: str | None
    summary: str
    source_name: str
    source_reliability: str
    confidence: float
    direction: str
    available_at: datetime
    ingested_at: datetime
    temporal_class: str
    source_quote: str | None = None
    is_mock: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_ref": self.evidence_ref,
            "kind": self.kind,
            "subject_ref": self.subject_ref,
            "summary": self.summary,
            "source_name": self.source_name,
            "source_reliability": self.source_reliability,
            "confidence": self.confidence,
            "direction": self.direction,
            "available_at": self.available_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
            "temporal_class": self.temporal_class,
            "is_mock": self.is_mock,
        }


@dataclass(frozen=True)
class UnresolvedEvidenceSnapshot:
    """Detached projection of one ``UnresolvedLiveEvidence`` row.

    Surfaces evidence whose subject could not be resolved to a canonical player
    or team, so a report can warn about it instead of silently omitting it.
    """

    evidence_ref: str
    kind: str
    subject_hint: str | None
    resolution_status: str
    resolution_reason: str
    quote: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_ref": self.evidence_ref,
            "kind": self.kind,
            "subject_hint": self.subject_hint,
            "resolution_status": self.resolution_status,
            "resolution_reason": self.resolution_reason,
        }


def snapshot_evidence(
    db: Session,
    *,
    extraction_run_id: int | None,
    availability_evidence_ids: list[int],
    view: LedgerItemView,
    is_mock: bool,
) -> list[IngestedEvidenceSnapshot]:
    """Project the evidence written by one extraction run into detached snapshots.

    Must be called while the writing transaction is still open (the rows are
    flushed but not necessarily committed). Everything it returns is a plain
    frozen dataclass, so a subsequent ``rollback()`` cannot invalidate it.
    """
    if extraction_run_id is None:
        return []

    snapshots: list[IngestedEvidenceSnapshot] = []
    timestamps = view.timestamps
    temporal_class = view.temporal_class

    if availability_evidence_ids:
        for ev in db.scalars(
            select(AvailabilityEvidence)
            .where(AvailabilityEvidence.id.in_(availability_evidence_ids))
            .order_by(AvailabilityEvidence.id)
        ).all():
            snapshots.append(
                IngestedEvidenceSnapshot(
                    evidence_ref=f"avail:{ev.id}",
                    kind="availability",
                    subject_ref=f"player:{ev.player_id}",
                    summary=ev.description or f"{ev.evidence_type}: {ev.status_mentioned}",
                    source_name=view.source_name,
                    source_reliability=view.source_reliability,
                    confidence=float(ev.confidence),
                    direction="unknown",
                    available_at=timestamps.available_at,
                    ingested_at=timestamps.ingested_at,
                    temporal_class=temporal_class,
                    source_quote=ev.description,
                    is_mock=is_mock,
                )
            )

    for tac in db.scalars(
        select(TacticalEvidence)
        .where(TacticalEvidence.extraction_run_id == extraction_run_id)
        .order_by(TacticalEvidence.id)
    ).all():
        if tac.player_id is not None:
            subject_ref: str | None = f"player:{tac.player_id}"
        elif tac.team_id is not None:
            subject_ref = f"team:{tac.team_id}"
        else:
            subject_ref = None
        snapshots.append(
            IngestedEvidenceSnapshot(
                evidence_ref=f"tact:{tac.id}",
                kind="tactical",
                subject_ref=subject_ref,
                summary=(
                    tac.value_text or tac.description or tac.source_quote or str(tac.evidence_type)
                ),
                source_name=view.source_name,
                source_reliability=view.source_reliability,
                confidence=float(tac.confidence),
                direction=str(tac.direction),
                available_at=timestamps.available_at,
                ingested_at=timestamps.ingested_at,
                temporal_class=temporal_class,
                source_quote=tac.source_quote,
                is_mock=is_mock,
            )
        )

    return snapshots


def snapshot_unresolved(
    db: Session, extraction_run_id: int | None
) -> list[UnresolvedEvidenceSnapshot]:
    """Project this run's ``UnresolvedLiveEvidence`` rows into detached snapshots."""
    if extraction_run_id is None:
        return []
    return [
        UnresolvedEvidenceSnapshot(
            evidence_ref=f"unresolved:{row.id}",
            kind="availability" if row.status_mentioned else "tactical",
            subject_hint=row.player_name or row.team_name,
            resolution_status=str(row.resolution_status),
            resolution_reason=row.resolution_reason or "unknown",
            quote=row.quote,
        )
        for row in db.scalars(
            select(UnresolvedLiveEvidence)
            .where(UnresolvedLiveEvidence.extraction_run_id == extraction_run_id)
            .order_by(UnresolvedLiveEvidence.id)
        ).all()
    ]


def collect_evidence_ids(db: Session, extraction_run_id: int | None) -> tuple[list[int], list[int]]:
    """Return ``(availability_evidence_ids, tactical_evidence_ids)`` for a run."""
    if extraction_run_id is None:
        return [], []
    avail = list(
        db.scalars(
            select(LiveAvailabilityEvidenceLink.availability_evidence_id).where(
                LiveAvailabilityEvidenceLink.extraction_run_id == extraction_run_id
            )
        ).all()
    )
    tactical = list(
        db.scalars(
            select(TacticalEvidence.id).where(
                TacticalEvidence.extraction_run_id == extraction_run_id
            )
        ).all()
    )
    return list(avail), list(tactical)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class ManualIngestStatus(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass
class ManualIngestReport:
    """Outcome of one manual ingestion, returned to the CLI and tests."""

    status: ManualIngestStatus
    source_id: str
    content_hash: str
    raw_item_id: int | None = None
    extraction_run_id: int | None = None
    availability_count: int = 0
    tactical_count: int = 0
    availability_evidence_ids: list[int] = field(default_factory=list)
    tactical_evidence_ids: list[int] = field(default_factory=list)
    resolved_count: int = 0
    unresolved_count: int = 0
    ambiguous_count: int = 0
    unresolved_evidence_ids: list[int] = field(default_factory=list)
    duplicate: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "source_id": self.source_id,
            "content_hash": self.content_hash,
            "raw_item_id": self.raw_item_id,
            "extraction_run_id": self.extraction_run_id,
            "availability_count": self.availability_count,
            "tactical_count": self.tactical_count,
            "resolved_count": self.resolved_count,
            "unresolved_count": self.unresolved_count,
            "ambiguous_count": self.ambiguous_count,
            "availability_evidence_ids": self.availability_evidence_ids,
            "tactical_evidence_ids": self.tactical_evidence_ids,
            "unresolved_evidence_ids": self.unresolved_evidence_ids,
            "duplicate": self.duplicate,
            "error": self.error,
        }


def _resolve_deadline_context(
    db: Session,
    season_code: str | None,
    gameweek_number: int | None,
) -> tuple[int | None, int | None, datetime | None]:
    """Resolve season/gameweek and snapshot the deadline for context attachment."""
    season_id: int | None = None
    gameweek_id: int | None = None
    deadline_at: datetime | None = None

    if season_code:
        season = db.scalar(select(Season).where(Season.code == season_code))
        if season is not None:
            season_id = season.id
    if season_id is not None and gameweek_number is not None:
        gameweek = db.scalar(
            select(Gameweek).where(
                Gameweek.season_id == season_id,
                Gameweek.provider_event_id == gameweek_number,
            )
        )
        if gameweek is not None:
            gameweek_id = gameweek.id
            deadline_at = gameweek.deadline_time
            if deadline_at is not None and deadline_at.tzinfo is None:
                deadline_at = deadline_at.replace(tzinfo=UTC)
    return season_id, gameweek_id, deadline_at


def ingest_raw_text(
    db: Session,
    *,
    source_id: str,
    text: str,
    published_at: datetime,
    url: str | None = None,
    external_id: str | None = None,
    title: str | None = None,
    source_type: SourceType | None = None,
    scraped_at: datetime | None = None,
    ingested_at: datetime | None = None,
    available_at: datetime | None = None,
    temporal_class: str | None = None,
    season_code: str | None = None,
    gameweek_number: int | None = None,
    provider: LLMProvider | None = None,
    clock: Clock = utc_now,
    policy: InformationAccessPolicy = InformationAccessPolicy.STRICT_REPRODUCIBILITY,
    dry_run: bool = False,
) -> ManualIngestReport:
    """Ingest raw text: hash -> dedup -> ledger -> extract -> persist evidence.

    This is the controlled multi-source ingestion path. It is safe to call
    repeatedly with the same text + source: the second call is detected as a
    duplicate and returns without invoking the extractor.

    Args:
        db: Session to read from / write into.
        source_id: Phase 9.2 source identifier (e.g. ``"press_conference_manual"``).
        text: The raw unstructured content.
        published_at: When the source published it (timezone-aware).
        url: Optional source URL.
        external_id: Optional provider-side identifier for the content.
        title: Display title; defaults to the source id.
        source_type: Override the source type (else inferred from presets).
        scraped_at / ingested_at / available_at: Optional explicit timestamps;
            default to the pipeline clock (``ingested_at``/``scraped_at``) and to
            ``published_at`` (``available_at``).
        temporal_class: Explicit temporal class when no gameweek context is given.
        season_code / gameweek_number: Optional context that lets the row be
            classified against a real deadline (pre/post-deadline).
        provider: LLM provider for extraction. Defaults to a deterministic
            :class:`MockLLMProvider`, so the scaffold makes no network calls.
        clock / policy: Injected for determinism under test.
        dry_run: When True, roll back the session after extraction so no rows
            are permanently written. The returned report still carries all
            counts and IDs as if the run had been committed.

    Returns:
        A :class:`ManualIngestReport` summarising what happened.
    """
    now = clock()
    _require_aware("published_at", published_at)
    scraped = scraped_at or now
    ingested = ingested_at or now

    registry = SourceRegistry()
    source_db = registry.ensure_source(db, source_id, source_type=source_type)

    try:
        raw = RawItem.create(
            source_id=source_id,
            title=title or (url or source_id),
            content_text=text,
            published_at=published_at,
            scraped_at=scraped,
            ingested_at=ingested,
            url=url,
            external_id=external_id,
            available_at=available_at,
            temporal_class=temporal_class or RawTemporalClass.NO_DEADLINE_CONTEXT.value,
        )
    except ValidationError as exc:
        return ManualIngestReport(
            status=ManualIngestStatus.REJECTED,
            source_id=source_id,
            content_hash=content_hash(text),
            error=f"raw item failed temporal validation: {exc}",
        )

    ledger = RawItemLedger(db, clock=clock, policy=policy)
    if ledger.is_duplicate(source_db.id, raw.content_hash):
        return ManualIngestReport(
            status=ManualIngestStatus.DUPLICATE,
            source_id=source_id,
            content_hash=raw.content_hash,
            duplicate=True,
        )

    season_id, gameweek_id, deadline_at = _resolve_deadline_context(
        db, season_code, gameweek_number
    )
    resolved_temporal_class = temporal_class
    if season_id is not None and gameweek_id is not None and deadline_at is not None:
        timestamps = build_timestamps(
            scraped_at=scraped,
            ingested_at=ingested,
            published_at=published_at,
            availability_policy=AvailabilityDerivationPolicy.CONSERVATIVE,
            now=now,
        )
        resolved_temporal_class = str(classify_ledger_entry(timestamps, deadline_at, policy))
        raw = raw.model_copy(update={"temporal_class": resolved_temporal_class})

    item = ledger.persist(
        raw,
        source_db_id=source_db.id,
        season_id=season_id,
        gameweek_id=gameweek_id,
        deadline_at=deadline_at,
        external_id=external_id,
    )
    if item is None:  # race-free duplicate detected at write time
        return ManualIngestReport(
            status=ManualIngestStatus.DUPLICATE,
            source_id=source_id,
            content_hash=raw.content_hash,
            duplicate=True,
        )

    view = ledger.to_ledger_view(item)
    extractor = PromptedLLMExtractor(provider or MockLLMProvider())
    result = extractor.extract(view)

    resolver = build_entity_resolver(db)
    report: PersistenceReport = persist_extraction(
        db,
        result,
        season_id=season_id,
        gameweek_id=gameweek_id,
        resolve_player=resolver,
        resolve_team=resolver,
    )
    if dry_run:
        db.rollback()
    else:
        db.commit()

    avail_ids, tactical_ids = collect_evidence_ids(db, report.extraction_run_id)
    unresolved_ids = list(
        db.scalars(
            select(UnresolvedLiveEvidence.id).where(
                UnresolvedLiveEvidence.extraction_run_id == report.extraction_run_id
            )
        ).all()
    )
    return ManualIngestReport(
        status=ManualIngestStatus.CREATED,
        source_id=source_id,
        content_hash=raw.content_hash,
        raw_item_id=item.id,
        extraction_run_id=report.extraction_run_id,
        availability_count=report.availability_persisted,
        tactical_count=report.tactical_persisted,
        resolved_count=report.resolved,
        unresolved_count=report.unresolved_count,
        ambiguous_count=report.ambiguous_count,
        availability_evidence_ids=avail_ids,
        tactical_evidence_ids=tactical_ids,
        unresolved_evidence_ids=unresolved_ids,
    )
