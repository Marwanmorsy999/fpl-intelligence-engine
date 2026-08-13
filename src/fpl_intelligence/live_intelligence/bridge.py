"""Phase 9.4 — Quantitative Bridge and Evidence Query Layer.

Connects the AI Analyst (Phase 9.3) to the real quantitative engine
(Phases 4/5/6) and the live evidence database, so that intelligence reports
can be generated automatically from real predictions and stored evidence —
no manual CLI inputs for the ``PredictionContext`` required.

Three components
-----------------

1. **``PredictionContextBuilder``** — the *quantitative bridge*. Converts a
   :class:`~fpl_intelligence.optimization.provider.PlayerPrediction` from the
   frozen Phase 4/5/6 engine into the read-only
   :class:`~fpl_intelligence.live_intelligence.report.PredictionContext` the
   analyst is permitted to cite.

2. **``EvidenceQueryService``** — the *evidence query layer*. Queries the
   database for pre-deadline qualitative evidence (Phase 7 availability,
   Phase 8 tactical, Phase 9.2.1 unresolved) and returns it as
   :class:`~fpl_intelligence.live_intelligence.analyst.EvidenceCitation`
   objects, filtered by the gameweek cutoff time.

3. **``AnalystReportGenerator``** — the *orchestrator*. Builds the
   :class:`~fpl_intelligence.live_intelligence.report.PredictionContext`,
   queries evidence, and delegates to
   :class:`~fpl_intelligence.live_intelligence.analyst.AIAnalyst` to produce a
   validated :class:`~fpl_intelligence.live_intelligence.report.IntelligenceReport`.

Design rules
------------

* The quantitative engine is never modified — only read through the existing
  :class:`~fpl_intelligence.optimization.provider.DecisionPredictionProvider`
  interface.
* Evidence is filtered by the same :class:`InformationAccessPolicy` used
  elsewhere in the engine, so look-ahead is structurally impossible.
* Mock-environment evidence is excluded by default; the ``--dry-run`` path
  opts in via ``allow_mock_evidence=True``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.models import (
    AvailabilityEvidence,
    SourceReliability,
)
from fpl_intelligence.features.temporal import InformationAccessPolicy
from fpl_intelligence.live_intelligence.analyst import (
    AIAnalyst,
    AnalystTask,
    EvidenceCitation,
)
from fpl_intelligence.live_intelligence.extraction import LLMProvider
from fpl_intelligence.live_intelligence.models import (
    LedgerTemporalClass,
    LiveAvailabilityEvidenceLink,
    LiveIntelligenceRawItem,
    LiveIntelligenceSource,
    LLMExtractionRun,
    TacticalEvidence,
    UnresolvedLiveEvidence,
)
from fpl_intelligence.live_intelligence.report import (
    IntelligenceReport,
    PredictionContext,
)
from fpl_intelligence.live_intelligence.temporal_ledger import utc_now
from fpl_intelligence.optimization.provider import (
    DecisionPredictionProvider,
    PlayerPrediction,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime so comparisons do not raise."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _ensure_datetime(dt: datetime | None) -> datetime:
    """Return an aware UTC datetime, falling back to the current UTC time.

    The quantitative evidence tables model every timestamp as non-null, so the
    fallback only exists to satisfy the type checker about the ``datetime | None``
    output of :func:`_ensure_aware`.
    """
    return _ensure_aware(dt) or utc_now()


def _source_is_mock(source: LiveIntelligenceSource | None) -> bool:
    """True when the source is a mock/engineering artefact.

    A ``None`` source (Phase 7 historical evidence without a Phase 9 link) is
    treated as *real* — it predates the live accumulator and is not mock.
    """
    if source is None:
        return False
    return source.environment != "real"


def _run_is_mock(run: LLMExtractionRun | None) -> bool:
    """True when the LLM extraction run was performed by a test double."""
    return bool(run and run.is_mock)


def _safe_float(value: float | None, default: float = 0.5) -> float:
    """Coerce a nullable float DB column to a non-None float."""
    if value is None:
        return default
    return float(value)


def _safe_str(value: Any) -> str:
    """Coerce an enum or string to a plain string."""
    if value is None:
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# 1. Quantitative Bridge — PredictionContextBuilder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredictionContextBuilder:
    """Builds a :class:`~fpl_intelligence.live_intelligence.report.PredictionContext`
    from a :class:`~fpl_intelligence.optimization.provider.DecisionPredictionProvider`.

    This is the quantitative bridge: it converts the Phase 4/5/6 engine's
    :class:`~fpl_intelligence.optimization.provider.PlayerPrediction` into the
    read-only snapshot the analyst is permitted to cite. The prediction is
    projected into immutable values and the provider is never asked to change
    anything.
    """

    prediction_provider: DecisionPredictionProvider

    def build(
        self,
        player_id: int,
        gameweek: int,
        *,
        display_name: str | None = None,
        subject_ref: str | None = None,
    ) -> PredictionContext:
        """Build a :class:`PredictionContext` for one player + gameweek.

        Args:
            player_id: Canonical player ID.
            gameweek: FPL gameweek number.
            display_name: Optional human-readable label.
            subject_ref: Optional stable handle; defaults to ``player:{id}``.

        Returns:
            A populated, read-only :class:`PredictionContext`.
        """
        prediction = self.prediction_provider.get_player_prediction(player_id, gameweek)
        fixture_count = self.prediction_provider.get_fixture_count(player_id, gameweek)
        ref = subject_ref or f"player:{player_id}"
        return PredictionContext(
            player_id=player_id,
            gameweek=gameweek,
            expected_points=float(prediction.expected_points),
            expected_minutes=float(prediction.expected_minutes),
            start_probability=float(prediction.start_probability),
            floor=float(prediction.floor),
            ceiling=float(prediction.ceiling),
            model_confidence=float(prediction.confidence),
            fixture_count=fixture_count,
            subject_ref=ref,
            display_name=display_name,
        )


# ---------------------------------------------------------------------------
# 2. Evidence Query Service
# ---------------------------------------------------------------------------


@dataclass
class EvidenceQueryResult:
    """Filtered evidence for one player + cutoff, split by kind.

    Attributes:
        availability_evidence: Resolved Phase 7 availability citations.
        tactical_evidence: Resolved Phase 8 tactical citations.
        unresolved_evidence: Phase 9.2.1 unresolved-live citations.
    """

    availability_evidence: list[EvidenceCitation] = field(default_factory=list)
    tactical_evidence: list[EvidenceCitation] = field(default_factory=list)
    unresolved_evidence: list[EvidenceCitation] = field(default_factory=list)

    @property
    def all_citations(self) -> list[EvidenceCitation]:
        """Combined list of all citations, ready for the analyst."""
        return [
            *self.availability_evidence,
            *self.tactical_evidence,
            *self.unresolved_evidence,
        ]

    def __len__(self) -> int:
        return len(self.all_citations)


def _temporal_condition(
    available_at_col: Any,
    ingested_at_col: Any,
    cutoff: datetime,
    policy: InformationAccessPolicy,
) -> Any:
    """Build the SQL condition for 'available at or before cutoff' under *policy*."""
    cutoff = _ensure_aware(cutoff) or cutoff
    if policy == InformationAccessPolicy.PUBLIC_AVAILABILITY:
        return available_at_col <= cutoff
    if policy == InformationAccessPolicy.SYSTEM_AVAILABILITY:
        return ingested_at_col <= cutoff
        # STRICT_REPRODUCIBILITY (default)
    return and_(available_at_col <= cutoff, ingested_at_col <= cutoff)


class EvidenceQueryService:
    """Queries the evidence database for pre-deadline qualitative evidence.

    Args:
        db: An open SQLAlchemy :class:`~sqlalchemy.orm.Session`.
        policy: Information-access policy for temporal filtering. Defaults to
            :attr:`InformationAccessPolicy.STRICT_REPRODUCIBILITY`.
        allow_mock: When ``True``, evidence from mock-environment sources is
            included. Defaults to ``False`` (only real evidence).
    """

    def __init__(
        self,
        db: Session,
        *,
        policy: InformationAccessPolicy = InformationAccessPolicy.STRICT_REPRODUCIBILITY,
        allow_mock: bool = False,
    ) -> None:
        self._db = db
        self._policy = policy
        self._allow_mock = allow_mock

    # -- public API --------------------------------------------------------

    def query_evidence(
        self,
        player_id: int,
        gameweek: int,
        cutoff_time: datetime,
    ) -> EvidenceQueryResult:
        """Query pre-deadline evidence for a player + cutoff.

        Args:
            player_id: Canonical player ID.
            gameweek: FPL gameweek number (for optional scope filtering).
            cutoff_time: The gameweek deadline; only evidence whose
                ``available_at`` (and ``ingested_at`` under strict
                reproducibility) is at or before this instant is returned.

        Returns:
            An :class:`EvidenceQueryResult` with filtered evidence lists.
        """
        cutoff = _ensure_aware(cutoff_time)
        logger.debug(
            "query_evidence: player_id=%s gw=%s cutoff=%s policy=%s allow_mock=%s",
            player_id,
            gameweek,
            cutoff.isoformat() if cutoff else None,
            self._policy,
            self._allow_mock,
        )
        return EvidenceQueryResult(
            availability_evidence=self._query_availability(player_id, cutoff),
            tactical_evidence=self._query_tactical(player_id, cutoff),
            unresolved_evidence=self._query_unresolved(cutoff),
        )

    # -- Phase 7 availability evidence ------------------------------------

    def _query_availability(
        self,
        player_id: int,
        cutoff: datetime | None,
    ) -> list[EvidenceCitation]:
        """Query resolved Phase 7 ``AvailabilityEvidence`` for a player.

        Availability evidence lives in the Phase 7 table and is linked to the
        Phase 9 ledger via :class:`LiveAvailabilityEvidenceLink`. We outer-join
        so that Phase 7 historical evidence (which has no link row) is still
        returned — its ``extracted_at`` is used as a temporal fallback.
        """
        ae = AvailabilityEvidence
        link = LiveAvailabilityEvidenceLink
        raw = LiveIntelligenceRawItem
        src = LiveIntelligenceSource

        # Temporal fallback: use the raw item's timestamps when a link exists,
        # otherwise fall back to the evidence's own ``extracted_at``.
        avail_col = func.coalesce(raw.available_at, ae.extracted_at)
        ingest_col = func.coalesce(raw.ingested_at, ae.extracted_at)

        stmt = (
            select(ae, link, raw, src)
            .outerjoin(link, link.availability_evidence_id == ae.id)
            .outerjoin(raw, raw.id == link.raw_item_id)
            .outerjoin(src, src.id == raw.source_id)
            .where(
                ae.player_id == player_id,
                ae.is_active.is_(True),
                _temporal_condition(avail_col, ingest_col, cutoff or utc_now(), self._policy),
            )
        )
        if not self._allow_mock:
            # Real sources *or* Phase-7 historical evidence with no source.
            stmt = stmt.where(or_(src.environment == "real", src.id.is_(None)))

        rows = self._db.execute(stmt).all()
        citations: list[EvidenceCitation] = []
        for ev, ev_link, ev_raw, ev_src in rows:
            if ev_raw is not None:
                available_at = _ensure_datetime(ev_raw.available_at) or _ensure_datetime(
                    ev.extracted_at
                )
                ingested_at = _ensure_datetime(ev_raw.ingested_at) or _ensure_datetime(
                    ev.extracted_at
                )
            else:
                available_at = _ensure_datetime(ev.extracted_at)
                ingested_at = _ensure_datetime(ev.extracted_at)

            temporal_class = str(
                (ev_link.temporal_class if ev_link else None)
                or (ev_raw.temporal_class if ev_raw else None)
                or LedgerTemporalClass.NO_DEADLINE_CONTEXT
            )

            citations.append(
                EvidenceCitation(
                    evidence_ref=f"avail:{ev.id}",
                    kind="availability",
                    summary=ev.description
                    or f"{ev.evidence_type}: {ev.status_mentioned or 'unknown'}",
                    source_name=ev_src.name if ev_src else "phase7_historical",
                    source_reliability=(
                        _safe_str(ev_src.reliability)
                        if ev_src
                        else str(SourceReliability.UNVERIFIED.value)
                    ),
                    confidence=_safe_float(ev.confidence),
                    available_at=available_at,
                    ingested_at=ingested_at,
                    temporal_class=temporal_class,
                    direction="unknown",
                    subject_ref=f"player:{ev.player_id}",
                    source_quote=(ev_link.source_quote if ev_link else ev.description),
                    is_mock=_source_is_mock(ev_src),
                )
            )
        return citations

    # -- Phase 8 tactical evidence ---------------------------------------

    def _query_tactical(
        self,
        player_id: int,
        cutoff: datetime | None,
    ) -> list[EvidenceCitation]:
        """Query resolved Phase 8 ``TacticalEvidence`` for a player.

        Tactical evidence carries its own temporal fields (``available_at``,
        ``ingested_at``) and joins directly to a raw item for the source.
        """
        te = TacticalEvidence
        raw = LiveIntelligenceRawItem
        src = LiveIntelligenceSource

        stmt = (
            select(te, raw, src)
            .join(raw, raw.id == te.raw_item_id)
            .join(src, src.id == raw.source_id)
            .where(
                te.player_id == player_id,
                te.is_active.is_(True),
                _temporal_condition(
                    te.available_at, te.ingested_at, cutoff or utc_now(), self._policy
                ),
            )
        )
        if not self._allow_mock:
            stmt = stmt.where(src.environment == "real")

        rows = self._db.execute(stmt).all()
        citations: list[EvidenceCitation] = []
        for ev, _ev_raw, ev_src in rows:
            subject_ref: str | None
            if ev.player_id:
                subject_ref = f"player:{ev.player_id}"
            elif ev.team_id:
                subject_ref = f"team:{ev.team_id}"
            else:
                subject_ref = None

            citations.append(
                EvidenceCitation(
                    evidence_ref=f"tact:{ev.id}",
                    kind="tactical",
                    summary=ev.description or ev.value_text or ev.evidence_type,
                    source_name=ev_src.name,
                    source_reliability=_safe_str(ev_src.reliability),
                    confidence=_safe_float(ev.confidence),
                    available_at=_ensure_datetime(ev.available_at),
                    ingested_at=_ensure_datetime(ev.ingested_at),
                    temporal_class=str(
                        ev.temporal_class or LedgerTemporalClass.NO_DEADLINE_CONTEXT
                    ),
                    direction=str(ev.direction),
                    subject_ref=subject_ref,
                    source_quote=ev.source_quote,
                    is_mock=_source_is_mock(ev_src),
                )
            )
        return citations

    # -- Phase 9.2.1 unresolved live evidence -----------------------------

    def _query_unresolved(
        self,
        cutoff: datetime | None,
    ) -> list[EvidenceCitation]:
        """Query Phase 9.2.1 ``UnresolvedLiveEvidence`` up to the cutoff.

        Unresolved evidence does not carry a ``player_id`` (the entity could
        not be resolved), so it cannot be filtered by player. All unresolved
        evidence that was available before the cutoff is returned; the analyst
        will only cite it when relevant.
        """
        ue = UnresolvedLiveEvidence
        raw = LiveIntelligenceRawItem
        src = LiveIntelligenceSource

        stmt = (
            select(ue, raw, src)
            .join(raw, raw.id == ue.raw_item_id)
            .join(src, src.id == ue.source_id)
            .where(
                _temporal_condition(
                    raw.available_at, raw.ingested_at, cutoff or utc_now(), self._policy
                ),
            )
        )
        if not self._allow_mock:
            stmt = stmt.where(src.environment == "real")

        rows = self._db.execute(stmt).all()
        citations: list[EvidenceCitation] = []
        for ev, ev_raw, ev_src in rows:
            summary = (
                ev.quote
                or ev.player_name
                or ev.team_name
                or ev.evidence_type
                or "(unresolved entity)"
            )

            citations.append(
                EvidenceCitation(
                    evidence_ref=f"unresolved:{ev.id}",
                    kind="unresolved",
                    summary=summary,
                    source_name=ev_src.name,
                    source_reliability=_safe_str(ev_src.reliability),
                    confidence=_safe_float(ev.confidence),
                    available_at=_ensure_datetime(ev_raw.available_at),
                    ingested_at=_ensure_datetime(ev_raw.ingested_at),
                    temporal_class=str(
                        ev_raw.temporal_class or LedgerTemporalClass.NO_DEADLINE_CONTEXT
                    ),
                    direction="unknown",
                    subject_ref=None,
                    source_quote=ev.quote,
                    is_mock=_source_is_mock(ev_src),
                )
            )
        return citations


# ---------------------------------------------------------------------------
# 3. Analyst Report Generator
# ---------------------------------------------------------------------------


class AnalystReportGenerator:
    """Orchestrates the flow from player + gameweek to IntelligenceReport.

    Wires together: (1) PredictionContextBuilder, (2) EvidenceQueryService,
    (3) AIAnalyst. When no evidence is found, the analyst produces a neutral
    report.
    """

    def __init__(
        self,
        prediction_builder: PredictionContextBuilder,
        evidence_service: EvidenceQueryService,
        provider: LLMProvider,
        *,
        task: AnalystTask = AnalystTask.TRANSFER_RECOMMENDATION,
        strict_leakage: bool = True,
        allow_mock_evidence: bool = True,
        policy: InformationAccessPolicy = InformationAccessPolicy.STRICT_REPRODUCIBILITY,
    ) -> None:
        self._prediction_builder = prediction_builder
        self._evidence_service = evidence_service
        self._provider = provider
        self._task = task
        self._strict_leakage = strict_leakage
        self._allow_mock_evidence = allow_mock_evidence
        self._policy = policy

    @property
    def prediction_builder(self) -> PredictionContextBuilder:
        return self._prediction_builder

    @property
    def evidence_service(self) -> EvidenceQueryService:
        return self._evidence_service

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    def generate(
        self,
        player_id: int,
        gameweek: int,
        *,
        cutoff_time: datetime | None = None,
        subject_label: str | None = None,
        notes: str = "",
    ) -> IntelligenceReport:
        """Generate a full IntelligenceReport for one player.

        Args:
            player_id: Canonical player ID.
            gameweek: FPL gameweek number.
            cutoff_time: Gameweek deadline. Defaults to current UTC time.
            subject_label: Human-readable player label.
            notes: Free-form notes injected into the analyst context.

        Returns:
            A validated IntelligenceReport.
        """
        deadline = _ensure_datetime(cutoff_time) if cutoff_time else utc_now()

        prediction = self._prediction_builder.build(
            player_id,
            gameweek,
            display_name=subject_label,
        )

        evidence_result = self._evidence_service.query_evidence(player_id, gameweek, deadline)
        evidence = evidence_result.all_citations

        if not evidence:
            logger.info(
                "No evidence found for player %s gw %s - neutral report.",
                player_id,
                gameweek,
            )

        analyst = AIAnalyst(
            self._provider,
            policy=self._policy,
            strict_leakage=self._strict_leakage,
            allow_mock_evidence=self._allow_mock_evidence,
        )

        return analyst.generate_report(
            prediction=prediction,
            evidence=evidence,
            task=self._task,
            deadline=deadline,
            subject_label=subject_label,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# 4. Static prediction provider (dry-run / testing only)
# ---------------------------------------------------------------------------


class StaticPredictionProvider(DecisionPredictionProvider):
    """A simple DecisionPredictionProvider that returns fixed predictions.

    Intended for --dry-run CLI mode and unit tests. It does not call any
    external API, scrape any web page, or read any database.
    """

    def __init__(
        self,
        *,
        expected_points: float = 5.5,
        expected_minutes: float = 60.0,
        start_probability: float = 0.8,
        floor: float = 2.0,
        ceiling: float = 10.0,
        confidence: float = 0.9,
        fixture_count: int = 1,
    ) -> None:
        self._ep = expected_points
        self._em = expected_minutes
        self._sp = start_probability
        self._floor = floor
        self._ceiling = ceiling
        self._conf = confidence
        self._fc = fixture_count

    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        import numpy as np

        return PlayerPrediction(
            player_id=player_id,
            gameweek=gameweek,
            expected_points=self._ep,
            expected_minutes=self._em,
            start_probability=self._sp,
            distribution=np.array([float(self._ep)]),
            floor=self._floor,
            ceiling=self._ceiling,
            confidence=self._conf,
        )

    def get_squad_predictions(
        self, squad_players: list[int], gameweeks: list[int]
    ) -> dict[int, dict[int, PlayerPrediction]]:
        import numpy as np  # noqa: F401

        return {
            gw: {pid: self.get_player_prediction(pid, gw) for pid in squad_players}
            for gw in gameweeks
        }

    def get_all_predictions(self, gameweek: int) -> dict[int, PlayerPrediction]:
        return {}

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        return self._fc
