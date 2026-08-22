"""Phase 9 live ingestion pipeline scaffold.

Accepts already-captured raw text (press conferences, tweets, articles,
transcripts) and writes it into the temporal ledger. It deliberately does
**not** scrape: acquisition is a separate, later concern, and keeping it out
means the ledger's temporal contract can be tested exhaustively without any
network dependency.

What the pipeline guarantees
----------------------------

* ``ingested_at`` always comes from the injected pipeline clock. A caller
  cannot supply it, so a row can never be back-dated into a window it did not
  belong to.
* ``available_at`` is derived, never accepted from the caller.
* Re-submitting identical text from the same source is a no-op
  (``IngestionStatus.DUPLICATE``), so a cron that re-reads the same page does
  not inflate the ledger.
* A submission that violates a temporal invariant is rejected with a reason and
  recorded as such; it is not silently coerced into something plausible.
* Gameweek/season resolution is optional. An unresolved row is stored as
  ``NO_DEADLINE_CONTEXT`` and is unusable until a deadline is attached — the
  safe default.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.models import SourceReliability
from fpl_intelligence.db.models import Gameweek, Season
from fpl_intelligence.domain.environment import DataEnvironment
from fpl_intelligence.features.temporal import InformationAccessPolicy
from fpl_intelligence.live_intelligence.models import (
    CaptureMethod,
    LedgerTemporalClass,
    LiveIntelligenceRawItem,
    LiveIntelligenceSource,
    LiveSourceType,
)
from fpl_intelligence.live_intelligence.temporal_ledger import (
    AvailabilityDerivationPolicy,
    Clock,
    TemporalIntegrityError,
    TemporalLedger,
    build_timestamps,
    classify_ledger_entry,
    content_hash,
    utc_now,
)


class IngestionStatus(StrEnum):
    """Outcome of a single submission."""

    CREATED = "created"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True)
class RawTextSubmission:
    """One unit of captured unstructured text offered to the ledger.

    Note what is *absent*: there is no ``ingested_at`` and no ``available_at``.
    Both are pipeline-owned. The submitter may only state what it observed —
    when the source published (if known) and when it captured.
    """

    source_name: str
    raw_text: str
    scraped_at: datetime
    title: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    event_time: datetime | None = None
    season_code: str | None = None
    gameweek_number: int | None = None
    team_hint: str | None = None
    player_hints: Sequence[str] = ()
    content_type: str = "text"
    language: str = "en"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestionOutcome:
    """Result of ingesting one submission."""

    status: IngestionStatus
    raw_item_id: int | None = None
    content_hash: str | None = None
    temporal_class: str | None = None
    deadline_at: datetime | None = None
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status is IngestionStatus.CREATED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "raw_item_id": self.raw_item_id,
            "content_hash": self.content_hash,
            "temporal_class": self.temporal_class,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "reason": self.reason,
        }


@dataclass
class IngestionReport:
    """Aggregate result over a batch of submissions."""

    created: int = 0
    duplicates: int = 0
    rejected: int = 0
    pre_deadline: int = 0
    post_deadline: int = 0
    no_deadline_context: int = 0
    outcomes: list[IngestionOutcome] = field(default_factory=list)

    def record(self, outcome: IngestionOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.status is IngestionStatus.CREATED:
            self.created += 1
        elif outcome.status is IngestionStatus.DUPLICATE:
            self.duplicates += 1
        else:
            self.rejected += 1

        if outcome.temporal_class == LedgerTemporalClass.PRE_DEADLINE:
            self.pre_deadline += 1
        elif outcome.temporal_class == LedgerTemporalClass.POST_DEADLINE:
            self.post_deadline += 1
        elif outcome.temporal_class == LedgerTemporalClass.NO_DEADLINE_CONTEXT:
            self.no_deadline_context += 1

    @property
    def submitted(self) -> int:
        return self.created + self.duplicates + self.rejected

    def conservation_ok(self) -> bool:
        """Every submission must land in exactly one bucket."""
        return self.submitted == len(self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "created": self.created,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "pre_deadline": self.pre_deadline,
            "post_deadline": self.post_deadline,
            "no_deadline_context": self.no_deadline_context,
            "conservation_ok": self.conservation_ok(),
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


class LiveIngestionPipeline:
    """Scaffold that turns raw text submissions into temporal ledger rows.

    Args:
        db: Session to write into.
        clock: Injected pipeline clock. Every ``ingested_at`` comes from here.
        policy: Information-access policy used to classify deadline eligibility.
        default_environment: Environment marker applied to sources created
            through :meth:`register_source` without an explicit override.
            Defaults to ``MOCK`` — a source must be *deliberately* declared real.
    """

    def __init__(
        self,
        db: Session,
        *,
        clock: Clock = utc_now,
        policy: InformationAccessPolicy = InformationAccessPolicy.STRICT_REPRODUCIBILITY,
        default_environment: DataEnvironment = DataEnvironment.MOCK,
    ) -> None:
        self._db = db
        self._clock = clock
        self._policy = policy
        self._default_environment = default_environment
        self._ledger = TemporalLedger(db, clock=clock, policy=policy)

    @property
    def ledger(self) -> TemporalLedger:
        return self._ledger

    # -- source registry ---------------------------------------------------

    def register_source(
        self,
        name: str,
        *,
        source_type: LiveSourceType = LiveSourceType.OTHER,
        reliability: SourceReliability = SourceReliability.UNVERIFIED,
        capture_method: CaptureMethod = CaptureMethod.MANUAL_PASTE,
        url: str | None = None,
        is_official_club: bool = False,
        environment: DataEnvironment | None = None,
        publication_timestamp_trusted: bool = False,
        notes: str = "",
    ) -> LiveIntelligenceSource:
        """Register (or return) a live source. Idempotent on ``name``.

        Re-registering an existing name returns the stored row unchanged rather
        than mutating its reliability or environment: source metadata is an
        auditable declaration, not a per-call parameter.
        """
        existing = self._db.scalar(
            select(LiveIntelligenceSource).where(LiveIntelligenceSource.name == name)
        )
        if existing is not None:
            return existing

        env = environment or self._default_environment
        source = LiveIntelligenceSource(
            name=name,
            source_type=source_type,
            reliability=reliability,
            capture_method=capture_method,
            url=url,
            is_official_club=is_official_club,
            environment=env.value,
            publication_timestamp_trusted=publication_timestamp_trusted,
            notes=notes or None,
            created_at=self._clock(),
        )
        self._db.add(source)
        self._db.flush()
        return source

    # -- ingestion ---------------------------------------------------------

    def ingest(self, submission: RawTextSubmission) -> IngestionOutcome:
        """Ingest a single submission into the temporal ledger."""
        if not submission.raw_text or not submission.raw_text.strip():
            return IngestionOutcome(
                status=IngestionStatus.REJECTED,
                reason="empty raw_text: nothing to ledger",
            )

        source = self._db.scalar(
            select(LiveIntelligenceSource).where(
                LiveIntelligenceSource.name == submission.source_name
            )
        )
        if source is None:
            return IngestionOutcome(
                status=IngestionStatus.REJECTED,
                reason=(
                    f"unknown source '{submission.source_name}': register it first so "
                    "its reliability and environment are declared before any row is "
                    "written"
                ),
            )

        digest = content_hash(submission.raw_text)
        duplicate = self._ledger.find_by_hash(source.id, digest)
        if duplicate is not None:
            return IngestionOutcome(
                status=IngestionStatus.DUPLICATE,
                raw_item_id=duplicate.id,
                content_hash=digest,
                temporal_class=duplicate.temporal_class,
                deadline_at=duplicate.deadline_at,
                reason="identical content already ledgered for this source",
            )

        now = self._clock()
        derivation = (
            AvailabilityDerivationPolicy.PUBLICATION_TRUSTED
            if source.publication_timestamp_trusted
            else AvailabilityDerivationPolicy.CONSERVATIVE
        )
        try:
            timestamps = build_timestamps(
                scraped_at=submission.scraped_at,
                ingested_at=now,
                published_at=submission.published_at,
                event_time=submission.event_time,
                availability_policy=derivation,
                now=now,
            )
        except TemporalIntegrityError as exc:
            return IngestionOutcome(
                status=IngestionStatus.REJECTED,
                content_hash=digest,
                reason=f"temporal integrity violation: {exc}",
            )

        season_id, gameweek_id, deadline_at = self._resolve_context(submission)
        temporal_class = classify_ledger_entry(timestamps, deadline_at, self._policy)

        item = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=digest,
            title=submission.title,
            raw_text=submission.raw_text,
            url=submission.url,
            content_type=submission.content_type,
            language=submission.language,
            team_hint=submission.team_hint,
            player_hints=(
                json.dumps(list(submission.player_hints)) if submission.player_hints else None
            ),
            season_id=season_id,
            gameweek_id=gameweek_id,
            event_time=timestamps.event_time,
            published_at=timestamps.published_at,
            scraped_at=timestamps.scraped_at,
            available_at=timestamps.available_at,
            ingested_at=timestamps.ingested_at,
            publication_established=timestamps.publication_established,
            deadline_at=deadline_at,
            temporal_class=temporal_class,
            access_policy=str(self._policy),
            metadata_json=json.dumps(submission.metadata) if submission.metadata else None,
        )
        self._db.add(item)
        self._db.flush()

        return IngestionOutcome(
            status=IngestionStatus.CREATED,
            raw_item_id=item.id,
            content_hash=digest,
            temporal_class=temporal_class,
            deadline_at=deadline_at,
        )

    def ingest_many(self, submissions: Iterable[RawTextSubmission]) -> IngestionReport:
        """Ingest a batch, returning a conservation-checked report."""
        report = IngestionReport()
        for submission in submissions:
            report.record(self.ingest(submission))
        return report

    # -- context resolution -------------------------------------------------

    def _resolve_context(
        self, submission: RawTextSubmission
    ) -> tuple[int | None, int | None, datetime | None]:
        """Resolve season/gameweek and snapshot the deadline.

        Resolution failure is not an error: the row is still ledgered, just
        with ``NO_DEADLINE_CONTEXT``, which makes it unusable rather than
        wrongly usable.
        """
        season_id: int | None = None
        gameweek_id: int | None = None
        deadline_at: datetime | None = None

        if submission.season_code:
            season = self._db.scalar(select(Season).where(Season.code == submission.season_code))
            season_id = season.id if season else None

        if season_id is not None and submission.gameweek_number is not None:
            gameweek = self._db.scalar(
                select(Gameweek).where(
                    Gameweek.season_id == season_id,
                    Gameweek.provider_event_id == submission.gameweek_number,
                )
            )
            if gameweek is not None:
                gameweek_id = gameweek.id
                deadline_at = gameweek.deadline_time
                if deadline_at is not None and deadline_at.tzinfo is None:
                    from datetime import UTC

                    deadline_at = deadline_at.replace(tzinfo=UTC)

        return season_id, gameweek_id, deadline_at
