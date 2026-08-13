"""Phase 9.4 unit tests — Quantitative Bridge and Evidence Query Layer.

Tests cover:
- PredictionContextBuilder (quantitative bridge)
- EvidenceQueryResult dataclass
- EvidenceQueryService (availability, tactical, unresolved evidence queries)
- AnalystReportGenerator (end-to-end orchestration)
- StaticPredictionProvider (dry-run provider)
- Helper functions (_ensure_aware, _safe_str, _safe_float, _source_is_mock,
  _run_is_mock, _temporal_condition)
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.availability.models import (
    AvailabilityEvidence,
    AvailabilityStatus,
    EvidenceType,
    SourceReliability,
)
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import Player, Season
from fpl_intelligence.domain.environment import DataEnvironment
from fpl_intelligence.features.temporal import InformationAccessPolicy
from fpl_intelligence.live_intelligence.analyst import (
    AnalystTask,
    EvidenceCitation,
)
from fpl_intelligence.live_intelligence.bridge import (
    AnalystReportGenerator,
    EvidenceQueryResult,
    EvidenceQueryService,
    PredictionContextBuilder,
    StaticPredictionProvider,
    _ensure_aware,
    _run_is_mock,
    _safe_float,
    _safe_str,
    _source_is_mock,
    _temporal_condition,
)
from fpl_intelligence.live_intelligence.mock_llm import make_mock_provider
from fpl_intelligence.live_intelligence.models import (
    CaptureMethod,
    LedgerTemporalClass,
    LiveAvailabilityEvidenceLink,
    LiveIntelligenceRawItem,
    LiveIntelligenceSource,
    LiveSourceType,
    LLMExtractionRun,
    ResolutionStatus,
    TacticalDirection,
    TacticalEvidence,
    TacticalEvidenceType,
    UnresolvedLiveEvidence,
)
from fpl_intelligence.live_intelligence.report import (
    IntelligenceReport,
    PredictionContext,
)
from fpl_intelligence.optimization.provider import (
    DecisionPredictionProvider,
    PlayerPrediction,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def real_source(db_session: Session) -> LiveIntelligenceSource:
    src = LiveIntelligenceSource(
        name="real_journalist",
        source_type=LiveSourceType.JOURNALIST,
        reliability=SourceReliability.VERIFIED_JOURNALIST,
        capture_method=CaptureMethod.MANUAL_PASTE,
        environment=DataEnvironment.REAL.value,
        publication_timestamp_trusted=False,
    )
    db_session.add(src)
    db_session.flush()
    return src


@pytest.fixture
def mock_source(db_session: Session) -> LiveIntelligenceSource:
    src = LiveIntelligenceSource(
        name="mock_source",
        source_type=LiveSourceType.JOURNALIST,
        reliability=SourceReliability.UNVERIFIED,
        capture_method=CaptureMethod.MOCK_FIXTURE,
        environment=DataEnvironment.MOCK.value,
        publication_timestamp_trusted=False,
    )
    db_session.add(src)
    db_session.flush()
    return src


@pytest.fixture
def real_raw_item(
    db_session: Session, real_source: LiveIntelligenceSource
) -> LiveIntelligenceRawItem:
    raw = LiveIntelligenceRawItem(
        source_id=real_source.id,
        content_hash="abc123real",
        raw_text="Salah will start against City.",
        scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
        temporal_class=LedgerTemporalClass.PRE_DEADLINE,
    )
    db_session.add(raw)
    db_session.flush()
    return raw


@pytest.fixture
def mock_raw_item(
    db_session: Session, mock_source: LiveIntelligenceSource
) -> LiveIntelligenceRawItem:
    raw = LiveIntelligenceRawItem(
        source_id=mock_source.id,
        content_hash="xyz789mock",
        raw_text="Salah is doubtful.",
        scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
        temporal_class=LedgerTemporalClass.PRE_DEADLINE,
    )
    db_session.add(raw)
    db_session.flush()
    return raw


@pytest.fixture
def season(db_session: Session) -> Season:
    s = Season(code="2025-26", display_name="2025/26")
    db_session.add(s)
    db_session.flush()
    return s


@pytest.fixture
def player(db_session: Session) -> Player:
    p = Player(first_name="Mohamed", second_name="Salah", web_name="Salah")
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture
def deadline() -> datetime:
    return _utc(datetime(2025, 8, 17, 18, 30, 0))


def _make_citation(**kwargs: Any) -> EvidenceCitation:
    defaults = dict(
        evidence_ref="ev_1",
        kind="availability",
        summary="test",
        source_name="src",
        source_reliability="unverified",
        confidence=0.5,
        available_at=_utc(datetime(2025, 8, 15)),
        ingested_at=_utc(datetime(2025, 8, 15)),
        temporal_class=LedgerTemporalClass.PRE_DEADLINE,
    )
    defaults.update(kwargs)
    return EvidenceCitation(**defaults)


def _compile(cond: Any) -> str:
    """Render a SQLAlchemy predicate to SQL with literals inlined."""
    return str(cond.compile(compile_kwargs={"literal_binds": True}))


def _make_raw(
    db_session: Session,
    source: LiveIntelligenceSource,
    *,
    content_hash: str,
    available_at: datetime,
    ingested_at: datetime,
    raw_text: str = "Rotation news about the squad.",
) -> LiveIntelligenceRawItem:
    raw = LiveIntelligenceRawItem(
        source_id=source.id,
        content_hash=content_hash,
        raw_text=raw_text,
        scraped_at=available_at,
        available_at=available_at,
        ingested_at=ingested_at,
        temporal_class=LedgerTemporalClass.PRE_DEADLINE,
    )
    db_session.add(raw)
    db_session.flush()
    return raw


def _add_availability(
    db_session: Session,
    *,
    player_id: int,
    season_id: int,
    extracted_at: datetime,
    description: str = "Salah reporting a hamstring doubt.",
    is_active: bool = True,
) -> AvailabilityEvidence:
    ev = AvailabilityEvidence(
        player_id=player_id,
        season_id=season_id,
        evidence_type=EvidenceType.INJURY,
        status_mentioned=AvailabilityStatus.OUT,
        confidence=0.8,
        description=description,
        extracted_at=extracted_at,
        is_active=is_active,
    )
    db_session.add(ev)
    db_session.flush()
    return ev


def _add_availability_link(
    db_session: Session,
    *,
    availability_evidence_id: int,
    raw_item_id: int,
) -> LiveAvailabilityEvidenceLink:
    link = LiveAvailabilityEvidenceLink(
        availability_evidence_id=availability_evidence_id,
        raw_item_id=raw_item_id,
        temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        source_quote="Salah reporting a hamstring doubt.",
    )
    db_session.add(link)
    db_session.flush()
    return link


def _add_tactical(
    db_session: Session,
    *,
    raw_item_id: int,
    player_id: int | None,
    available_at: datetime,
    ingested_at: datetime,
    direction: str = TacticalDirection.NEGATIVE,
    is_active: bool = True,
) -> TacticalEvidence:
    te = TacticalEvidence(
        raw_item_id=raw_item_id,
        player_id=player_id,
        evidence_type=TacticalEvidenceType.ROTATION_TENDENCY,
        value_text="rotation risk",
        direction=direction,
        confidence=0.7,
        source_quote="The squad is expected to rotate this weekend.",
        description="Rotation signal.",
        available_at=available_at,
        ingested_at=ingested_at,
        temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        is_active=is_active,
    )
    db_session.add(te)
    db_session.flush()
    return te


def _add_unresolved(
    db_session: Session,
    *,
    raw_item_id: int,
    source_id: int,
) -> UnresolvedLiveEvidence:
    ue = UnresolvedLiveEvidence(
        raw_item_id=raw_item_id,
        source_id=source_id,
        evidence_type="availability",
        player_name="Unknown Player",
        quote="A mystery winger is doubtful.",
        confidence=0.5,
        resolution_status=ResolutionStatus.UNRESOLVED_PLAYER,
    )
    db_session.add(ue)
    db_session.flush()
    return ue

# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_ensure_aware_returns_none(self):
        assert _ensure_aware(None) is None

    def test_ensure_aware_attaches_utc(self):
        naive = datetime(2025, 8, 15, 10, 0, 0)
        result = _ensure_aware(naive)
        assert result.tzinfo == UTC

    def test_ensure_aware_preserves_aware(self):
        aware = _utc(datetime(2025, 8, 15, 10, 0, 0))
        result = _ensure_aware(aware)
        assert result == aware

    def test_safe_str_none(self):
        assert _safe_str(None) == ""

    def test_safe_str_enum(self):
        assert _safe_str(SourceReliability.OFFICIAL) == "official"

    def test_safe_str_string(self):
        assert _safe_str("hello") == "hello"

    def test_safe_float_none(self):
        assert _safe_float(None) == 0.5

    def test_safe_float_none_custom_default(self):
        assert _safe_float(None, default=0.7) == 0.7

    def test_safe_float_value(self):
        assert _safe_float(0.9) == 0.9

    def test_source_is_mock_none_source(self):
        assert _source_is_mock(None) is False

    def test_source_is_mock_real(self, real_source: LiveIntelligenceSource):
        assert _source_is_mock(real_source) is False

    def test_source_is_mock_mock(self, mock_source: LiveIntelligenceSource):
        assert _source_is_mock(mock_source) is True

    def test_run_is_mock_none(self):
        assert _run_is_mock(None) is False

    def test_run_is_mock_true(self):
        run = LLMExtractionRun(
            raw_item_id=1,
            extractor_name="test",
            provider_name="mock",
            model_name="mock-v1",
            prompt_template_id="test",
            prompt_version="v1",
            prompt_hash="abc",
            schema_version="v1",
            is_mock=True,
            status="ok",
        )
        assert _run_is_mock(run) is True

    def test_temporal_condition_strict_reproducibility(self):
        cutoff = _utc(datetime(2025, 8, 17, 18, 30, 0))
        cond = _temporal_condition(
            LiveIntelligenceRawItem.available_at,
            LiveIntelligenceRawItem.ingested_at,
            cutoff,
            InformationAccessPolicy.STRICT_REPRODUCIBILITY,
        )
        sql = _compile(cond)
        assert "AND" in sql
        assert "available_at" in sql
        assert "ingested_at" in sql

    def test_temporal_condition_public_availability(self):
        cutoff = _utc(datetime(2025, 8, 17, 18, 30, 0))
        cond = _temporal_condition(
            LiveIntelligenceRawItem.available_at,
            LiveIntelligenceRawItem.ingested_at,
            cutoff,
            InformationAccessPolicy.PUBLIC_AVAILABILITY,
        )
        sql = _compile(cond)
        assert "available_at" in sql
        assert "ingested_at" not in sql

    def test_temporal_condition_system_availability(self):
        cutoff = _utc(datetime(2025, 8, 17, 18, 30, 0))
        cond = _temporal_condition(
            LiveIntelligenceRawItem.available_at,
            LiveIntelligenceRawItem.ingested_at,
            cutoff,
            InformationAccessPolicy.SYSTEM_AVAILABILITY,
        )
        sql = _compile(cond)
        assert "ingested_at" in sql
        assert "available_at" not in sql
# ---------------------------------------------------------------------------
# PredictionContextBuilder tests
# ---------------------------------------------------------------------------


class _StubPredictionProvider(DecisionPredictionProvider):
    """A configurable DecisionPredictionProvider double for builder tests."""

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
        self.predictions_calls: list[tuple[int, int]] = []
        self.fixture_calls: list[tuple[int, int]] = []
        self._ep = expected_points
        self._em = expected_minutes
        self._sp = start_probability
        self._floor = floor
        self._ceiling = ceiling
        self._conf = confidence
        self._fc = fixture_count

    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        self.predictions_calls.append((player_id, gameweek))
        return PlayerPrediction(
            player_id=player_id,
            gameweek=gameweek,
            expected_points=self._ep,
            expected_minutes=self._em,
            start_probability=self._sp,
            distribution=np.array([self._ep]),
            floor=self._floor,
            ceiling=self._ceiling,
            confidence=self._conf,
        )

    def get_squad_predictions(
        self, squad_players: list[int], gameweeks: list[int]
    ) -> dict[int, dict[int, PlayerPrediction]]:
        return {}

    def get_all_predictions(self, gameweek: int) -> dict[int, PlayerPrediction]:
        return {}

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        self.fixture_calls.append((player_id, gameweek))
        return self._fc


class TestPredictionContextBuilder:
    def test_build_populates_all_fields_from_provider(self):
        provider = _StubPredictionProvider(
            expected_points=6.25,
            expected_minutes=75.0,
            start_probability=0.65,
            floor=3.0,
            ceiling=12.0,
            confidence=0.85,
            fixture_count=2,
        )
        builder = PredictionContextBuilder(prediction_provider=provider)
        ctx = builder.build(1, 4)

        assert isinstance(ctx, PredictionContext)
        assert ctx.player_id == 1
        assert ctx.gameweek == 4
        assert ctx.expected_points == pytest.approx(6.25)
        assert ctx.expected_minutes == pytest.approx(75.0)
        assert ctx.start_probability == pytest.approx(0.65)
        assert ctx.floor == pytest.approx(3.0)
        assert ctx.ceiling == pytest.approx(12.0)
        assert ctx.model_confidence == pytest.approx(0.85)
        assert ctx.fixture_count == 2
        assert ctx.subject_ref == "player:1"

    def test_build_queries_provider_for_player_and_gameweek(self):
        provider = _StubPredictionProvider()
        builder = PredictionContextBuilder(prediction_provider=provider)
        builder.build(42, 7)

        assert provider.predictions_calls == [(42, 7)]
        assert provider.fixture_calls == [(42, 7)]

    def test_build_default_subject_ref_and_no_display_name(self):
        provider = _StubPredictionProvider()
        builder = PredictionContextBuilder(prediction_provider=provider)
        ctx = builder.build(1, 3)

        assert ctx.subject_ref == "player:1"
        assert ctx.display_name is None

    def test_build_with_custom_subject_ref(self):
        provider = _StubPredictionProvider()
        builder = PredictionContextBuilder(prediction_provider=provider)
        ctx = builder.build(1, 3, subject_ref="player:999")

        assert ctx.subject_ref == "player:999"

    def test_build_with_display_name(self):
        provider = _StubPredictionProvider()
        builder = PredictionContextBuilder(prediction_provider=provider)
        ctx = builder.build(1, 3, display_name="Mohamed Salah")

        assert ctx.display_name == "Mohamed Salah"

    def test_build_is_frozen_readonly_snapshot(self):
        provider = _StubPredictionProvider()
        builder = PredictionContextBuilder(prediction_provider=provider)
        ctx = builder.build(1, 1)

        with pytest.raises((AttributeError, Exception)):
            ctx.expected_points = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EvidenceQueryResult tests
# ---------------------------------------------------------------------------


class TestEvidenceQueryResult:
    def test_empty_result(self):
        result = EvidenceQueryResult()

        assert result.all_citations == []
        assert len(result) == 0

    def test_all_citations_concatenate_in_order(self):
        avail = _make_citation(evidence_ref="avail:1", kind="availability")
        tact = _make_citation(evidence_ref="tact:1", kind="tactical")
        unres = _make_citation(evidence_ref="unresolved:1", kind="unresolved")

        result = EvidenceQueryResult(
            availability_evidence=[avail],
            tactical_evidence=[tact],
            unresolved_evidence=[unres],
        )

        assert result.all_citations == [avail, tact, unres]
        assert len(result) == 3
# ---------------------------------------------------------------------------
# EvidenceQueryService tests
# ---------------------------------------------------------------------------


class TestEvidenceQueryService:
    def test_returns_all_kinds_in_one_query(
        self,
        db_session: Session,
        real_source: LiveIntelligenceSource,
        real_raw_item: LiveIntelligenceRawItem,
        season: Season,
        player: Player,
        deadline: datetime,
    ) -> None:
        avail = _add_availability(
            db_session,
            player_id=player.id,
            season_id=season.id,
            extracted_at=real_raw_item.available_at,
        )
        _add_availability_link(
            db_session,
            availability_evidence_id=avail.id,
            raw_item_id=real_raw_item.id,
        )
        tactic = _add_tactical(
            db_session,
            raw_item_id=real_raw_item.id,
            player_id=player.id,
            available_at=real_raw_item.available_at,
            ingested_at=real_raw_item.ingested_at,
        )
        unresolved = _add_unresolved(
            db_session,
            raw_item_id=real_raw_item.id,
            source_id=real_source.id,
        )

        service = EvidenceQueryService(db_session)
        result = service.query_evidence(player.id, 3, deadline)

        assert len(result.availability_evidence) == 1
        assert len(result.tactical_evidence) == 1
        assert len(result.unresolved_evidence) == 1
        assert len(result.all_citations) == 3

        avail_cit = result.availability_evidence[0]
        assert avail_cit.evidence_ref == f"avail:{avail.id}"
        assert avail_cit.kind == "availability"
        assert avail_cit.subject_ref == f"player:{player.id}"
        assert avail_cit.source_name == "real_journalist"
        assert avail_cit.is_mock is False

        tact_cit = result.tactical_evidence[0]
        assert tact_cit.evidence_ref == f"tact:{tactic.id}"
        assert tact_cit.kind == "tactical"
        assert tact_cit.direction == "negative"

        unres_cit = result.unresolved_evidence[0]
        assert unres_cit.evidence_ref == f"unresolved:{unresolved.id}"
        assert unres_cit.kind == "unresolved"
        assert unres_cit.subject_ref is None

    def test_cutoff_excludes_late_evidence(
        self,
        db_session: Session,
        real_source: LiveIntelligenceSource,
        season: Season,
        player: Player,
        deadline: datetime,
    ) -> None:
        early_raw = _make_raw(
            db_session,
            real_source,
            content_hash="early",
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
        )
        late_raw = _make_raw(
            db_session,
            real_source,
            content_hash="late",
            available_at=_utc(datetime(2025, 8, 20, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 20, 10, 5, 0)),
        )

        early_avail = _add_availability(
            db_session,
            player_id=player.id,
            season_id=season.id,
            extracted_at=early_raw.available_at,
        )
        _add_availability_link(
            db_session,
            availability_evidence_id=early_avail.id,
            raw_item_id=early_raw.id,
        )
        late_avail = _add_availability(
            db_session,
            player_id=player.id,
            season_id=season.id,
            extracted_at=late_raw.available_at,
        )
        _add_availability_link(
            db_session,
            availability_evidence_id=late_avail.id,
            raw_item_id=late_raw.id,
        )

        _add_tactical(
            db_session,
            raw_item_id=early_raw.id,
            player_id=player.id,
            available_at=early_raw.available_at,
            ingested_at=early_raw.ingested_at,
        )
        _add_tactical(
            db_session,
            raw_item_id=late_raw.id,
            player_id=player.id,
            available_at=late_raw.available_at,
            ingested_at=late_raw.ingested_at,
        )

        _add_unresolved(
            db_session,
            raw_item_id=early_raw.id,
            source_id=real_source.id,
        )
        _add_unresolved(
            db_session,
            raw_item_id=late_raw.id,
            source_id=real_source.id,
        )

        service = EvidenceQueryService(db_session)
        result = service.query_evidence(player.id, 3, deadline)

        refs = [c.evidence_ref for c in result.all_citations]
        assert f"avail:{early_avail.id}" in refs
        assert f"avail:{late_avail.id}" not in refs
        assert len(result.availability_evidence) == 1
        assert len(result.tactical_evidence) == 1
        assert len(result.unresolved_evidence) == 1


    def test_player_scoped_for_availability_and_tactical(
        self,
        db_session: Session,
        real_source: LiveIntelligenceSource,
        real_raw_item: LiveIntelligenceRawItem,
        season: Season,
        player: Player,
        deadline: datetime,
    ) -> None:
        other = Player(first_name="Other", second_name="Player", web_name="Other")
        db_session.add(other)
        db_session.flush()

        mine = _add_availability(
            db_session,
            player_id=player.id,
            season_id=season.id,
            extracted_at=real_raw_item.available_at,
        )
        _add_availability_link(
            db_session,
            availability_evidence_id=mine.id,
            raw_item_id=real_raw_item.id,
        )
        theirs = _add_availability(
            db_session,
            player_id=other.id,
            season_id=season.id,
            extracted_at=real_raw_item.available_at,
            description="Another player's injury.",
        )
        _add_availability_link(
            db_session,
            availability_evidence_id=theirs.id,
            raw_item_id=real_raw_item.id,
        )
        _add_tactical(
            db_session,
            raw_item_id=real_raw_item.id,
            player_id=player.id,
            available_at=real_raw_item.available_at,
            ingested_at=real_raw_item.ingested_at,
        )
        _add_unresolved(
            db_session,
            raw_item_id=real_raw_item.id,
            source_id=real_source.id,
        )

        service = EvidenceQueryService(db_session)
        result = service.query_evidence(player.id, 3, deadline)

        refs = [c.evidence_ref for c in result.all_citations]
        assert f"avail:{mine.id}" in refs
        assert f"avail:{theirs.id}" not in refs
        assert len(result.tactical_evidence) == 1
        # Unresolved evidence is not player-scoped (the entity could not resolve).
        assert len(result.unresolved_evidence) == 1

    def test_mock_evidence_excluded_by_default(
        self,
        db_session: Session,
        mock_source: LiveIntelligenceSource,
        mock_raw_item: LiveIntelligenceRawItem,
        season: Season,
        player: Player,
        deadline: datetime,
    ) -> None:
        ev = _add_availability(
            db_session,
            player_id=player.id,
            season_id=season.id,
            extracted_at=mock_raw_item.available_at,
        )
        _add_availability_link(
            db_session,
            availability_evidence_id=ev.id,
            raw_item_id=mock_raw_item.id,
        )
        _add_tactical(
            db_session,
            raw_item_id=mock_raw_item.id,
            player_id=player.id,
            available_at=mock_raw_item.available_at,
            ingested_at=mock_raw_item.ingested_at,
        )
        _add_unresolved(
            db_session,
            raw_item_id=mock_raw_item.id,
            source_id=mock_source.id,
        )

        strict_service = EvidenceQueryService(db_session)
        strict_result = strict_service.query_evidence(player.id, 3, deadline)
        assert strict_result.all_citations == []

        permissive_service = EvidenceQueryService(db_session, allow_mock=True)
        permissive_result = permissive_service.query_evidence(player.id, 3, deadline)
        assert len(permissive_result.all_citations) == 3
        assert all(c.is_mock is True for c in permissive_result.all_citations)

    def test_phase7_historical_evidence_without_link_is_included(
        self,
        db_session: Session,
        season: Season,
        player: Player,
        deadline: datetime,
    ) -> None:
        _add_availability(
            db_session,
            player_id=player.id,
            season_id=season.id,
            extracted_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        )

        service = EvidenceQueryService(db_session)
        result = service.query_evidence(player.id, 3, deadline)

        assert len(result.availability_evidence) == 1
        cit = result.availability_evidence[0]
        assert cit.source_name == "phase7_historical"
        assert cit.is_mock is False
        assert cit.source_reliability == "unverified"
        assert cit.temporal_class == "no_deadline_context"

    def test_inactive_evidence_excluded(
        self,
        db_session: Session,
        real_source: LiveIntelligenceSource,
        real_raw_item: LiveIntelligenceRawItem,
        season: Season,
        player: Player,
        deadline: datetime,
    ) -> None:
        _add_availability(
            db_session,
            player_id=player.id,
            season_id=season.id,
            extracted_at=real_raw_item.available_at,
            is_active=False,
        )
        _add_tactical(
            db_session,
            raw_item_id=real_raw_item.id,
            player_id=player.id,
            available_at=real_raw_item.available_at,
            ingested_at=real_raw_item.ingested_at,
            is_active=False,
        )

        service = EvidenceQueryService(db_session)
        result = service.query_evidence(player.id, 3, deadline)

        assert result.all_citations == []

# ---------------------------------------------------------------------------
# AnalystReportGenerator tests
# ---------------------------------------------------------------------------


@pytest.fixture
def evidence_db(
    db_session: Session,
    real_source: LiveIntelligenceSource,
    real_raw_item: LiveIntelligenceRawItem,
    season: Season,
    player: Player,
) -> Session:
    """A DB session populated with one pre-deadline evidence of each kind."""
    avail = _add_availability(
        db_session,
        player_id=player.id,
        season_id=season.id,
        extracted_at=real_raw_item.available_at,
    )
    _add_availability_link(
        db_session,
        availability_evidence_id=avail.id,
        raw_item_id=real_raw_item.id,
    )
    _add_tactical(
        db_session,
        raw_item_id=real_raw_item.id,
        player_id=player.id,
        available_at=real_raw_item.available_at,
        ingested_at=real_raw_item.ingested_at,
        direction=TacticalDirection.POSITIVE,
    )
    _add_unresolved(
        db_session,
        raw_item_id=real_raw_item.id,
        source_id=real_source.id,
    )
    return db_session


class TestAnalystReportGenerator:
    def _build_generator(
        self,
        db_session: Session,
        *,
        allow_mock_evidence: bool = True,
    ) -> AnalystReportGenerator:
        builder = PredictionContextBuilder(
            prediction_provider=StaticPredictionProvider(
                expected_points=5.5,
                expected_minutes=60.0,
                start_probability=0.8,
                floor=2.0,
                ceiling=10.0,
                confidence=0.9,
                fixture_count=1,
            )
        )
        evidence_service = EvidenceQueryService(db_session, allow_mock=False)
        provider = make_mock_provider(player_names=["Mohamed Salah"])
        return AnalystReportGenerator(
            builder,
            evidence_service,
            provider,
            task=AnalystTask.TRANSFER_RECOMMENDATION,
            strict_leakage=True,
            allow_mock_evidence=allow_mock_evidence,
        )

    def test_end_to_end_with_real_evidence(
        self,
        evidence_db: Session,
        player: Player,
        deadline: datetime,
    ) -> None:
        generator = self._build_generator(evidence_db)
        report = generator.generate(
            player.id,
            3,
            cutoff_time=deadline,
            subject_label="Mohamed Salah",
        )

        assert isinstance(report, IntelligenceReport)
        assert report.prediction_context.player_id == player.id
        assert report.prediction_context.gameweek == 3
        assert report.prediction_context.expected_points == pytest.approx(5.5)
        assert report.prediction_context.start_probability == pytest.approx(0.8)
        assert report.prediction_context.floor == pytest.approx(2.0)
        assert report.prediction_context.ceiling == pytest.approx(10.0)
        assert report.is_mock is True
        assert len(report.citations) == 3
        assert report.qualitative_adjustment.direction == "up"

        markdown = report.render_markdown()
        assert "Quantitative Baseline" in markdown
        assert "Qualitative Assessment" in markdown
        assert "Evidence Cited" in markdown


    def test_neutral_report_when_no_evidence(
        self,
        db_session: Session,
        deadline: datetime,
    ) -> None:
        generator = self._build_generator(db_session)
        report = generator.generate(1, 3, cutoff_time=deadline)

        assert report.citations == []
        assert report.qualitative_adjustment.direction == "neutral"
        assert report.qualitative_adjustment.magnitude == "none"
        assert report.recommendation == "no_recommendation"

    def test_mock_evidence_excluded_from_report(
        self,
        db_session: Session,
        mock_source: LiveIntelligenceSource,
        mock_raw_item: LiveIntelligenceRawItem,
        season: Season,
        player: Player,
        deadline: datetime,
    ) -> None:
        ev = _add_availability(
            db_session,
            player_id=player.id,
            season_id=season.id,
            extracted_at=mock_raw_item.available_at,
        )
        _add_availability_link(
            db_session,
            availability_evidence_id=ev.id,
            raw_item_id=mock_raw_item.id,
        )

        # Default EvidenceQueryService(allow_mock=False) already drops mock-red
        # evidence before the analyst is reached.
        generator = self._build_generator(db_session)
        report = generator.generate(player.id, 3, cutoff_time=deadline)

        assert report.citations == []
        assert report.qualitative_adjustment.direction == "neutral"
        assert report.recommendation == "no_recommendation"


# ---------------------------------------------------------------------------
# StaticPredictionProvider tests (dry-run / testing double)
# ---------------------------------------------------------------------------


class TestStaticPredictionProvider:
    def test_get_player_prediction(self):
        provider = StaticPredictionProvider()
        pred = provider.get_player_prediction(1, 2)

        assert pred.player_id == 1
        assert pred.gameweek == 2
        assert pred.expected_points == pytest.approx(5.5)
        assert pred.expected_minutes == pytest.approx(60.0)
        assert pred.start_probability == pytest.approx(0.8)
        assert pred.floor == pytest.approx(2.0)
        assert pred.ceiling == pytest.approx(10.0)
        assert pred.confidence == pytest.approx(0.9)

    def test_custom_values_and_fixture_count(self):
        provider = StaticPredictionProvider(
            expected_points=3.0,
            floor=1.0,
            ceiling=7.0,
            fixture_count=2,
        )
        pred = provider.get_player_prediction(9, 5)

        assert pred.expected_points == pytest.approx(3.0)
        assert provider.get_fixture_count(9, 5) == 2

    def test_get_squad_predictions(self):
        provider = StaticPredictionProvider()
        result = provider.get_squad_predictions([1, 2], [3, 4])

        assert set(result) == {3, 4}
        assert set(result[3]) == {1, 2}
        assert result[3][1].gameweek == 3

    def test_get_all_predictions_empty(self):
        provider = StaticPredictionProvider()
        assert provider.get_all_predictions(3) == {}

