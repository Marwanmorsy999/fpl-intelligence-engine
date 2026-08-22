"""Phase 9 LLM extraction engine — the reasoning layer's entry point.

Pipeline for one ledger row::

    LedgerItemView -> PromptTemplate.render -> LLMProvider.complete
                   -> strict JSON parse -> Pydantic validation
                   -> grounding check -> typed drafts (temporal fields inherited)
                   -> persistence (availability_evidence + tactical_evidence)

Failure is always recorded, never swallowed. A run that produced nothing has a
status explaining exactly which stage rejected it, which is what makes the
ledger auditable rather than merely populated.
"""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.models import AvailabilityEvidence
from fpl_intelligence.live_intelligence.entity_resolution import (
    ResolutionResult,
    ResolutionStatus,
)
from fpl_intelligence.live_intelligence.models import (
    ExtractionStatus,
    LedgerTemporalClass,
    LiveAvailabilityEvidenceLink,
    LiveIntelligenceRawItem,
    LLMExtractionRun,
    TacticalDirection,
    TacticalEvidence,
    UnresolvedLiveEvidence,
)
from fpl_intelligence.live_intelligence.prompt_registry import hash_prompt_template
from fpl_intelligence.live_intelligence.prompts import (
    COMBINED_EXTRACTION,
    LLMPrompt,
    PromptTemplate,
)
from fpl_intelligence.live_intelligence.schemas import (
    EXTRACTION_SCHEMA_VERSION,
    ExtractedAvailabilityEvidence,
    ExtractedTacticalEvidence,
    ExtractionEnvelope,
    quote_is_grounded,
)
from fpl_intelligence.live_intelligence.temporal_ledger import (
    Clock,
    LedgerItemView,
    LedgerTimestamps,
    utc_now,
)

# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class LLMProviderError(RuntimeError):
    """Raised by a provider when the underlying model call fails."""


@dataclass(frozen=True)
class LLMResponse:
    """Raw text returned by a provider, plus the provenance we must persist."""

    text: str
    provider_name: str
    model_name: str
    is_mock: bool
    latency_ms: int | None = None
    temperature: float | None = None
    #: True when the text was served from the Phase 9.1 response cache rather
    #: than fetched from the provider. A cached response cost no quota, and the
    #: distinction is recorded rather than hidden so free-tier usage can be
    #: audited honestly.
    from_cache: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    max_output_tokens: int | None = None
    finish_reason: str | None = None
    #: The routing strategy used to select this provider (Phase 9.1).
    #: Empty string when the provider was selected directly without routing.
    routing_strategy: str = ""


class LLMProvider(abc.ABC):
    """Abstract text-in / text-out model provider.

    Kept deliberately thin. The engine owns prompting, parsing, validation and
    grounding; a provider only has to return a string. That is what lets the
    mock provider be a genuine drop-in rather than a special case threaded
    through the engine with ``if is_mock`` branches.
    """

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Short identifier persisted on every extraction run."""

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """Model identifier persisted on every extraction run."""

    @property
    def is_mock(self) -> bool:
        """True for test doubles. Mock output can never become evidence."""
        return False

    @abc.abstractmethod
    def complete(self, prompt: LLMPrompt) -> LLMResponse:
        """Return the model's raw response for a rendered prompt."""


# ---------------------------------------------------------------------------
# Extraction results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RejectedItem:
    """An item the model produced that the engine refused to accept."""

    kind: str
    reason: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "payload": self.payload}


@dataclass(frozen=True)
class AvailabilityEvidenceDraft:
    """Validated availability evidence with ledger-inherited temporal fields.

    A *draft* because entity resolution has not happened yet. Persisting it
    requires a resolved ``player_id``; an unresolved draft is reported, not
    dropped.
    """

    player_name: str
    team_name: str | None
    evidence_type: str
    status_mentioned: str
    confidence: float
    expected_absence_gameweeks: int | None
    source_quote: str
    reasoning: str
    # Inherited, never LLM-supplied:
    raw_item_id: int | None
    published_at: datetime | None
    available_at: datetime
    ingested_at: datetime
    temporal_class: str
    # Method provenance (Phase 9.1), never LLM-supplied. Two extractions are
    # only comparable when these agree, so they travel with the evidence
    # rather than living only on the run row.
    prompt_hash: str | None = None
    prompt_template_id: str | None = None
    provider_name: str | None = None
    model_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_name": self.player_name,
            "team_name": self.team_name,
            "evidence_type": self.evidence_type,
            "status_mentioned": self.status_mentioned,
            "confidence": self.confidence,
            "expected_absence_gameweeks": self.expected_absence_gameweeks,
            "source_quote": self.source_quote,
            "temporal_class": self.temporal_class,
            "available_at": self.available_at.isoformat(),
            "prompt_hash": self.prompt_hash,
            "prompt_template_id": self.prompt_template_id,
            "provider_name": self.provider_name,
            "model_name": self.model_name,
        }


@dataclass(frozen=True)
class TacticalEvidenceDraft:
    """Validated tactical evidence with ledger-inherited temporal fields."""

    evidence_type: str
    team_name: str | None
    player_name: str | None
    value_text: str | None
    numeric_value: float | None
    direction: str
    confidence: float
    source_quote: str
    reasoning: str
    # Inherited, never LLM-supplied:
    raw_item_id: int | None
    published_at: datetime | None
    available_at: datetime
    ingested_at: datetime
    temporal_class: str
    # Method provenance (Phase 9.1), never LLM-supplied.
    prompt_hash: str | None = None
    prompt_template_id: str | None = None
    provider_name: str | None = None
    model_name: str | None = None

    @property
    def subject_hint(self) -> str | None:
        return self.player_name or self.team_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_type": self.evidence_type,
            "team_name": self.team_name,
            "player_name": self.player_name,
            "value_text": self.value_text,
            "numeric_value": self.numeric_value,
            "direction": self.direction,
            "confidence": self.confidence,
            "source_quote": self.source_quote,
            "temporal_class": self.temporal_class,
            "available_at": self.available_at.isoformat(),
            "prompt_hash": self.prompt_hash,
            "prompt_template_id": self.prompt_template_id,
            "provider_name": self.provider_name,
            "model_name": self.model_name,
        }


@dataclass(frozen=True)
class ExtractionProvenance:
    """Everything needed to reproduce or audit one extraction call."""

    extractor_name: str
    provider_name: str
    model_name: str
    template_id: str
    template_version: str
    prompt_hash: str
    schema_version: str
    is_mock: bool
    temperature: float | None = None
    latency_ms: int | None = None
    requested_at: datetime | None = None
    completed_at: datetime | None = None
    #: SHA-256 of the *unrendered* template (see :mod:`prompt_registry`).
    #: ``prompt_hash`` identifies this exact call; ``template_hash`` identifies
    #: the prompt design that produced it, across every input.
    template_hash: str = ""
    from_cache: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    max_output_tokens: int | None = None
    #: The routing strategy used to select the provider (Phase 9.1).
    #: Empty string when the provider was selected directly without routing.
    routing_strategy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "extractor_name": self.extractor_name,
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "template_hash": self.template_hash,
            "prompt_hash": self.prompt_hash,
            "schema_version": self.schema_version,
            "is_mock": self.is_mock,
            "from_cache": self.from_cache,
            "temperature": self.temperature,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "max_output_tokens": self.max_output_tokens,
            "routing_strategy": self.routing_strategy,
        }


@dataclass
class ExtractionResult:
    """Outcome of extracting one ledger row."""

    raw_item_id: int | None
    status: ExtractionStatus
    provenance: ExtractionProvenance
    envelope: ExtractionEnvelope | None = None
    availability: list[AvailabilityEvidenceDraft] = field(default_factory=list)
    tactical: list[TacticalEvidenceDraft] = field(default_factory=list)
    rejected: list[RejectedItem] = field(default_factory=list)
    raw_response: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (ExtractionStatus.OK, ExtractionStatus.EMPTY)

    @property
    def accepted_count(self) -> int:
        return len(self.availability) + len(self.tactical)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_item_id": self.raw_item_id,
            "status": str(self.status),
            "availability": [d.to_dict() for d in self.availability],
            "tactical": [d.to_dict() for d in self.tactical],
            "rejected": [r.to_dict() for r in self.rejected],
            "error": self.error,
            "provider": self.provenance.provider_name,
            "model": self.provenance.model_name,
            "is_mock": self.provenance.is_mock,
            "prompt_hash": self.provenance.prompt_hash,
        }


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class LLMExtractor(abc.ABC):
    """Turns one ledger row into typed, grounded, temporally-inherited evidence."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Identifier persisted on every extraction run."""

    @abc.abstractmethod
    def extract(self, item: LedgerItemView) -> ExtractionResult:
        """Extract structured evidence from a single ledger row."""

    def extract_many(self, items: list[LedgerItemView]) -> list[ExtractionResult]:
        """Extract over a batch, preserving input order."""
        return [self.extract(item) for item in items]


class PromptedLLMExtractor(LLMExtractor):
    """Concrete extractor: render template, call provider, validate, ground.

    Args:
        provider: Any :class:`LLMProvider`, real or mock.
        template: Prompt template to render. Defaults to the combined
            availability + tactics template.
        clock: Injected clock, so latency and ``extracted_at`` are deterministic
            under test.
        require_grounding: When True (the default) an item whose ``source_quote``
            is not a literal substring of the ledger text is rejected. Turning
            this off is only meaningful for prompt development and is never
            appropriate for evidence that will be persisted.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        template: PromptTemplate = COMBINED_EXTRACTION,
        clock: Clock = utc_now,
        require_grounding: bool = True,
        name: str = "phase9.prompted_extractor",
    ) -> None:
        self._provider = provider
        self._template = template
        self._clock = clock
        self._require_grounding = require_grounding
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    def build_prompt(self, item: LedgerItemView) -> LLMPrompt:
        """Render the prompt for a ledger row.

        Only the row's own text and source metadata are injected. No outcome
        data, no other ledger rows, no database state — the prompt physically
        cannot contain post-deadline information.
        """
        return self._template.render(
            context={
                "raw_text": item.raw_text,
                "source_name": item.source_name,
                "source_type": item.source_type,
                "source_reliability": item.source_reliability,
                "team_hint": item.team_hint or "unknown",
            },
            raw_text=item.raw_text,
            source_name=item.source_name,
            source_type=item.source_type,
            source_reliability=item.source_reliability,
            team_hint=item.team_hint or "unknown",
        )

    def extract(self, item: LedgerItemView) -> ExtractionResult:
        prompt = self.build_prompt(item)
        requested_at = self._clock()
        template_hash = hash_prompt_template(self._template)

        def _provenance(
            response: LLMResponse | None, completed_at: datetime | None
        ) -> ExtractionProvenance:
            return ExtractionProvenance(
                extractor_name=self._name,
                provider_name=(
                    response.provider_name if response else self._provider.provider_name
                ),
                model_name=response.model_name if response else self._provider.model_name,
                template_id=prompt.template_id,
                template_version=prompt.version,
                prompt_hash=prompt.hash(),
                template_hash=template_hash,
                schema_version=EXTRACTION_SCHEMA_VERSION,
                is_mock=response.is_mock if response else self._provider.is_mock,
                temperature=response.temperature if response else None,
                latency_ms=response.latency_ms if response else None,
                requested_at=requested_at,
                completed_at=completed_at,
                from_cache=response.from_cache if response else False,
                prompt_tokens=response.prompt_tokens if response else None,
                completion_tokens=response.completion_tokens if response else None,
                max_output_tokens=response.max_output_tokens if response else None,
                routing_strategy=response.routing_strategy if response else "",
            )

        # 1. Provider call.
        try:
            response = self._provider.complete(prompt)
        except LLMProviderError as exc:
            return ExtractionResult(
                raw_item_id=item.raw_item_id,
                status=ExtractionStatus.PROVIDER_ERROR,
                provenance=_provenance(None, self._clock()),
                error=str(exc),
            )

        completed_at = self._clock()
        provenance = _provenance(response, completed_at)

        # 2. Strict JSON parse.
        try:
            payload = json.loads(strip_code_fence(response.text))
        except (json.JSONDecodeError, TypeError) as exc:
            return ExtractionResult(
                raw_item_id=item.raw_item_id,
                status=ExtractionStatus.PARSE_FAILED,
                provenance=provenance,
                raw_response=response.text,
                error=f"response is not valid JSON: {exc}",
            )

        # 3. Schema validation (extra keys forbidden, enums enforced).
        try:
            envelope = ExtractionEnvelope.model_validate(payload)
        except ValidationError as exc:
            return ExtractionResult(
                raw_item_id=item.raw_item_id,
                status=ExtractionStatus.SCHEMA_REJECTED,
                provenance=provenance,
                raw_response=response.text,
                error=f"response failed schema validation: {exc.error_count()} error(s); "
                f"{exc.errors(include_url=False)[:3]}",
            )

        # 4. Grounding + temporal inheritance.
        availability, tactical, rejected = self._ground_and_inherit(item, envelope, provenance)

        if not availability and not tactical:
            status = ExtractionStatus.GROUNDING_REJECTED if rejected else ExtractionStatus.EMPTY
        else:
            status = ExtractionStatus.OK

        return ExtractionResult(
            raw_item_id=item.raw_item_id,
            status=status,
            provenance=provenance,
            envelope=envelope,
            availability=availability,
            tactical=tactical,
            rejected=rejected,
            raw_response=response.text,
        )

    # -- internals ---------------------------------------------------------

    def _ground_and_inherit(
        self,
        item: LedgerItemView,
        envelope: ExtractionEnvelope,
        provenance: ExtractionProvenance,
    ) -> tuple[list[AvailabilityEvidenceDraft], list[TacticalEvidenceDraft], list[RejectedItem]]:
        """Reject ungrounded items; stamp survivors with the ledger's timestamps.

        This is where the no-look-ahead guarantee is realised: the drafts take
        ``published_at`` / ``available_at`` / ``ingested_at`` / ``temporal_class``
        from ``item``, and there is no code path by which a model-supplied value
        could reach them. The same applies to the method provenance
        (``prompt_hash``, ``provider_name``): it comes from ``provenance``, which
        the engine computed, never from the model's payload.
        """
        ts: LedgerTimestamps = item.timestamps
        availability: list[AvailabilityEvidenceDraft] = []
        tactical: list[TacticalEvidenceDraft] = []
        rejected: list[RejectedItem] = []

        for claim in envelope.availability_evidence:
            if self._require_grounding and not quote_is_grounded(claim.source_quote, item.raw_text):
                rejected.append(
                    RejectedItem(
                        kind="availability",
                        reason="source_quote is not a literal substring of the ledger text",
                        payload=claim.model_dump(mode="json"),
                    )
                )
                continue
            availability.append(_to_availability_draft(claim, item, ts, provenance))

        for tac_claim in envelope.tactical_evidence:
            tac: ExtractedTacticalEvidence = tac_claim
            if self._require_grounding and not quote_is_grounded(tac.source_quote, item.raw_text):
                rejected.append(
                    RejectedItem(
                        kind="tactical",
                        reason="source_quote is not a literal substring of the ledger text",
                        payload=tac.model_dump(mode="json"),
                    )
                )
                continue
            if tac.player_name is None and tac.team_name is None:
                rejected.append(
                    RejectedItem(
                        kind="tactical",
                        reason="tactical evidence names neither a player nor a team",
                        payload=tac.model_dump(mode="json"),
                    )
                )
                continue
            tactical.append(_to_tactical_draft(tac, item, ts, provenance))

        return availability, tactical, rejected


def _to_availability_draft(
    claim: ExtractedAvailabilityEvidence,
    item: LedgerItemView,
    ts: LedgerTimestamps,
    provenance: ExtractionProvenance,
) -> AvailabilityEvidenceDraft:
    return AvailabilityEvidenceDraft(
        player_name=claim.player_name,
        team_name=claim.team_name,
        evidence_type=str(claim.evidence_type),
        status_mentioned=str(claim.status_mentioned),
        confidence=claim.confidence,
        expected_absence_gameweeks=claim.expected_absence_gameweeks,
        source_quote=claim.source_quote,
        reasoning=claim.reasoning,
        raw_item_id=item.raw_item_id,
        published_at=ts.published_at,
        available_at=ts.available_at,
        ingested_at=ts.ingested_at,
        temporal_class=item.temporal_class,
        prompt_hash=provenance.prompt_hash,
        prompt_template_id=provenance.template_id,
        provider_name=provenance.provider_name,
        model_name=provenance.model_name,
    )


def _to_tactical_draft(
    claim: ExtractedTacticalEvidence,
    item: LedgerItemView,
    ts: LedgerTimestamps,
    provenance: ExtractionProvenance,
) -> TacticalEvidenceDraft:
    return TacticalEvidenceDraft(
        evidence_type=str(claim.evidence_type),
        team_name=claim.team_name,
        player_name=claim.player_name,
        value_text=claim.value_text,
        numeric_value=claim.numeric_value,
        direction=str(claim.direction or TacticalDirection.UNKNOWN),
        confidence=claim.confidence,
        source_quote=claim.source_quote,
        reasoning=claim.reasoning,
        raw_item_id=item.raw_item_id,
        published_at=ts.published_at,
        available_at=ts.available_at,
        ingested_at=ts.ingested_at,
        temporal_class=item.temporal_class,
        prompt_hash=provenance.prompt_hash,
        prompt_template_id=provenance.template_id,
        provider_name=provenance.provider_name,
        model_name=provenance.model_name,
    )


def strip_code_fence(text: str) -> str:
    """Tolerate a ```json fence, which many models add despite instructions.

    Tolerating formatting noise is not the same as tolerating semantic noise:
    the content inside still has to satisfy the schema and the grounding check.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    body = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
    return "\n".join(body).strip()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


#: The resolver returns a :class:`ResolutionResult` (status + canonical id +
#: reason). Kept as a typing alias so callers can name it; the historical
#: ``Callable[[str, str | None], int | None]`` contract is deliberately widened.
EntityResolver = Any


@dataclass
class PersistenceReport:
    """What actually reached the database, and what did not."""

    extraction_run_id: int | None = None
    availability_persisted: int = 0
    tactical_persisted: int = 0
    resolved: int = 0
    unresolved_count: int = 0
    ambiguous_count: int = 0
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    unresolved_evidence_ids: list[int] = field(default_factory=list)
    skipped_duplicates: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "extraction_run_id": self.extraction_run_id,
            "availability_persisted": self.availability_persisted,
            "tactical_persisted": self.tactical_persisted,
            "resolved": self.resolved,
            "unresolved": self.unresolved_count,
            "ambiguous": self.ambiguous_count,
            "unresolved_evidence_ids": self.unresolved_evidence_ids,
            "skipped_duplicates": self.skipped_duplicates,
        }


def persist_extraction(
    db: Session,
    result: ExtractionResult,
    *,
    season_id: int | None = None,
    gameweek_id: int | None = None,
    resolve_player: EntityResolver = None,
    resolve_team: EntityResolver = None,
    clock: Clock = utc_now,
) -> PersistenceReport:
    """Write an extraction run and its accepted evidence to the database.

    Entity resolution is injected rather than assumed. Phase 7 was blocked
    precisely because an importer resolved against a different provider key
    than the ingestion path used, so here an unresolvable entity produces an
    ``unresolved`` entry on the run — and a persisted
    :class:`UnresolvedLiveEvidence` row — visible in every audit — instead of a
    guessed id or a silent drop.

    The injected resolver returns a :class:`ResolutionResult`. ``provider``,
    ``prompt_hash`` and ``team_hint`` are threaded onto the unresolved rows for
    auditability.

    ``availability_evidence.player_id`` is NOT NULL, so an unresolved
    availability draft cannot be persisted at all; that fact is recorded rather
    than worked around.
    """
    report = PersistenceReport()
    now = clock()
    prov = result.provenance

    run = LLMExtractionRun(
        raw_item_id=result.raw_item_id,
        extractor_name=prov.extractor_name,
        provider_name=prov.provider_name,
        model_name=prov.model_name,
        prompt_template_id=prov.template_id,
        prompt_version=prov.template_version,
        prompt_hash=prov.prompt_hash,
        prompt_template_hash=prov.template_hash or None,
        schema_version=prov.schema_version,
        temperature=prov.temperature,
        is_mock=prov.is_mock,
        from_cache=prov.from_cache,
        prompt_tokens=prov.prompt_tokens,
        completion_tokens=prov.completion_tokens,
        max_output_tokens=prov.max_output_tokens,
        routing_strategy=prov.routing_strategy or None,
        status=result.status,
        error=result.error,
        raw_response=result.raw_response,
        rejected_count=len(result.rejected),
        requested_at=prov.requested_at or now,
        completed_at=prov.completed_at,
        latency_ms=prov.latency_ms,
    )
    db.add(run)
    db.flush()
    report.extraction_run_id = run.id

    unresolved: list[dict[str, Any]] = [r.to_dict() for r in result.rejected]

    def _resolve_player(entity: str | None, team: str | None = None) -> ResolutionResult:
        if resolve_player is None:
            return ResolutionResult(
                ResolutionStatus.UNRESOLVED_PLAYER,
                None,
                "player could not be resolved to a canonical id",
            )
        raw = resolve_player(entity, team)
        if isinstance(raw, ResolutionResult):
            return raw
        if raw is None:
            return ResolutionResult(
                ResolutionStatus.UNRESOLVED_PLAYER,
                None,
                "player could not be resolved to a canonical id",
            )
        return ResolutionResult(ResolutionStatus.RESOLVED, int(raw), "resolved by legacy resolver")

    def _resolve_team(entity: str | None, team: str | None = None) -> ResolutionResult:
        if resolve_team is None:
            return ResolutionResult(ResolutionStatus.UNRESOLVED_TEAM, None, "no resolver")
        # New resolvers accept a ``kind`` kwarg; legacy ones do not.
        try:
            raw = resolve_team(entity, team, kind="team")
        except TypeError:
            raw = resolve_team(entity, team)
        if isinstance(raw, ResolutionResult):
            return raw
        if raw is None:
            return ResolutionResult(
                ResolutionStatus.UNRESOLVED_TEAM,
                None,
                "team could not be resolved to a canonical id",
            )
        return ResolutionResult(ResolutionStatus.RESOLVED, int(raw), "resolved by legacy resolver")

    # -- availability evidence (Phase 7 table + Phase 9 provenance link) ----
    raw_item = (
        db.scalar(
            select(LiveIntelligenceRawItem).where(LiveIntelligenceRawItem.id == result.raw_item_id)
        )
        if result.raw_item_id is not None
        else None
    )
    source_id = raw_item.source_id if raw_item is not None else None
    for draft in result.availability:
        res = _resolve_player(draft.player_name, draft.team_name)
        if res.resolved and season_id is not None:
            report.resolved += 1
            evidence = AvailabilityEvidence(
                player_id=res.canonical_id,
                season_id=season_id,
                gameweek_id=gameweek_id,
                evidence_type=draft.evidence_type,
                status_mentioned=draft.status_mentioned,
                confidence=draft.confidence,
                description=draft.source_quote,
                extracted_at=now,
                valid_from=draft.available_at,
                is_active=True,
            )
            db.add(evidence)
            db.flush()
            db.add(
                LiveAvailabilityEvidenceLink(
                    availability_evidence_id=evidence.id,
                    raw_item_id=draft.raw_item_id,
                    extraction_run_id=run.id,
                    source_quote=draft.source_quote,
                    temporal_class=draft.temporal_class,
                    prompt_hash=draft.prompt_hash,
                    provider_name=draft.provider_name,
                    model_name=draft.model_name,
                    created_at=now,
                )
            )
            report.availability_persisted += 1
        else:
            report.unresolved_count += 1
            if res.status is ResolutionStatus.AMBIGUOUS_PLAYER:
                report.ambiguous_count += 1
            row = UnresolvedLiveEvidence(
                raw_item_id=draft.raw_item_id,
                source_id=source_id,
                extraction_run_id=run.id,
                evidence_type=draft.evidence_type,
                player_name=draft.player_name,
                team_name=draft.team_name,
                status_mentioned=draft.status_mentioned,
                quote=draft.source_quote,
                confidence=draft.confidence,
                prompt_hash=draft.prompt_hash,
                provider_name=draft.provider_name,
                resolution_status=res.status,
                resolution_reason=res.reason,
            )
            db.add(row)
            db.flush()
            report.unresolved_evidence_ids.append(row.id)
            unresolved.append(
                {
                    "kind": "availability",
                    "reason": res.reason,
                    "resolution_status": str(res.status),
                    "payload": draft.to_dict(),
                }
            )

    # -- tactical evidence (Phase 9 table) ----------------------------------
    for tactical_draft in result.tactical:
        draft_t: TacticalEvidenceDraft = tactical_draft
        team_id: int | None = None
        if draft_t.team_name and resolve_team is not None:
            team_res = _resolve_team(draft_t.team_name, None)
            team_id = team_res.canonical_id
            if not team_res.resolved:
                report.unresolved_count += 1
                if team_res.status is ResolutionStatus.AMBIGUOUS_PLAYER:
                    report.ambiguous_count += 1
                _record_unresolved(db, report, run.id, source_id, draft_t, team_res, now)
        player_id: int | None = None
        if draft_t.player_name and resolve_player is not None:
            player_res = _resolve_player(draft_t.player_name, draft_t.team_name)
            player_id = player_res.canonical_id
            if player_res.resolved:
                report.resolved += 1
            else:
                report.unresolved_count += 1
                if player_res.status is ResolutionStatus.AMBIGUOUS_PLAYER:
                    report.ambiguous_count += 1
                _record_unresolved(db, report, run.id, source_id, draft_t, player_res, now)

        db.add(
            TacticalEvidence(
                raw_item_id=draft_t.raw_item_id,
                extraction_run_id=run.id,
                team_id=team_id,
                player_id=player_id,
                season_id=season_id,
                gameweek_id=gameweek_id,
                subject_hint=draft_t.subject_hint,
                evidence_type=draft_t.evidence_type,
                value_text=draft_t.value_text,
                numeric_value=draft_t.numeric_value,
                direction=draft_t.direction,
                confidence=draft_t.confidence,
                source_quote=draft_t.source_quote,
                description=draft_t.reasoning or None,
                published_at=draft_t.published_at,
                available_at=draft_t.available_at,
                ingested_at=draft_t.ingested_at,
                extracted_at=now,
                temporal_class=draft_t.temporal_class,
                prompt_hash=draft_t.prompt_hash,
                provider_name=draft_t.provider_name,
                model_name=draft_t.model_name,
                valid_from=draft_t.available_at,
                is_active=True,
            )
        )
        report.tactical_persisted += 1

    run.availability_evidence_count = report.availability_persisted
    run.tactical_evidence_count = report.tactical_persisted
    run.unresolved_entities = json.dumps(unresolved) if unresolved else None
    report.unresolved = unresolved
    db.flush()
    return report


def _record_unresolved(
    db: Session,
    report: PersistenceReport,
    run_id: int,
    source_id: int | None,
    draft: TacticalEvidenceDraft,
    res: ResolutionResult,
    now: datetime,
) -> None:
    """Persist one ``UnresolvedLiveEvidence`` row for an unresolved tactical draft."""
    row = UnresolvedLiveEvidence(
        raw_item_id=draft.raw_item_id,
        source_id=source_id,
        extraction_run_id=run_id,
        evidence_type=draft.evidence_type,
        player_name=draft.player_name,
        team_name=draft.team_name,
        status_mentioned=None,
        quote=draft.source_quote,
        confidence=draft.confidence,
        prompt_hash=draft.prompt_hash,
        provider_name=draft.provider_name,
        resolution_status=res.status,
        resolution_reason=res.reason,
    )
    db.add(row)
    db.flush()
    report.unresolved_evidence_ids.append(row.id)


def usable_drafts(
    result: ExtractionResult,
) -> tuple[list[AvailabilityEvidenceDraft], list[TacticalEvidenceDraft]]:
    """Filter an extraction result down to strictly pre-deadline drafts.

    The engine deliberately extracts from post-deadline rows too — the evidence
    is still historically interesting — but only ``PRE_DEADLINE`` material may
    ever reach a decision, and this is the sanctioned filter for that.
    """
    return (
        [d for d in result.availability if d.temporal_class == LedgerTemporalClass.PRE_DEADLINE],
        [d for d in result.tactical if d.temporal_class == LedgerTemporalClass.PRE_DEADLINE],
    )
