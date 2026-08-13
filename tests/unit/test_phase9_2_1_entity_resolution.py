"""Phase 9.2.1 — Entity resolution bridge and unresolved evidence persistence.

Covers:
* resolution by external id across multiple provider namespaces (alias tolerance)
* resolution by normalized name + team
* resolution by unique name
* ambiguous name handling
* unresolved player persistence (availability draft)
* unresolved team persistence (tactical draft)
* provider-key mismatch tolerance (real_fpl vs real_fpl_bootstrap)
* duplicate content still skipped
* raw item persisted even when evidence unresolved
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.availability.models import AvailabilityStatus, EvidenceType
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    Player,
    PlayerTeamMembership,
    Season,
    Team,
)
from fpl_intelligence.live_intelligence.entity_resolution import (
    CANONICAL_FPL_ELEMENT_KEY,
    ResolutionStatus,
    build_entity_resolver,
    canonical_provider_key,
    seed_fpl_external_id,
)
from fpl_intelligence.live_intelligence.extraction import (
    ExtractionProvenance,
    ExtractionResult,
    ExtractionStatus,
    persist_extraction,
)
from fpl_intelligence.live_intelligence.models import UnresolvedLiveEvidence
from fpl_intelligence.live_intelligence.raw_item_ledger import (
    ManualIngestStatus,
    ingest_raw_text,
)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _register_source(db_session: Session, source_id: str = "press_conference_manual") -> int:
    from fpl_intelligence.availability.models import SourceReliability
    from fpl_intelligence.live_intelligence.models import (
        CaptureMethod,
        LiveIntelligenceSource,
    )

    src = LiveIntelligenceSource(
        name=source_id,
        source_type="press_conference",
        reliability=SourceReliability.OFFICIAL,
        capture_method=CaptureMethod.MANUAL_PASTE,
        environment="real",
    )
    db_session.add(src)
    db_session.flush()
    return src.id


def _seed_players(db_session: Session):
    """One Liverpool (Salah) + one Arsenal (Saka), plus a duplicate-name case."""
    season = Season(code="2025-26", display_name="2025/26")
    db_session.add(season)
    db_session.flush()

    liverpool = Team(name="Liverpool")
    arsenal = Team(name="Arsenal")
    db_session.add_all([liverpool, arsenal])
    db_session.flush()

    salah = Player(first_name="Mohamed", second_name="Salah", web_name="Salah")
    saka = Player(first_name="Bukayo", second_name="Saka", web_name="Saka")
    # Two players both named "Smith" to force ambiguity.
    smith_a = Player(first_name="John", second_name="Smith", web_name="Smith")
    smith_b = Player(first_name="Jane", second_name="Smith", web_name="Smith")
    db_session.add_all([salah, saka, smith_a, smith_b])
    db_session.flush()

    db_session.add_all([
        PlayerTeamMembership(player_id=salah.id, team_id=liverpool.id, season_id=season.id),
        PlayerTeamMembership(player_id=saka.id, team_id=arsenal.id, season_id=season.id),
        PlayerTeamMembership(player_id=smith_a.id, team_id=liverpool.id, season_id=season.id),
        PlayerTeamMembership(player_id=smith_b.id, team_id=arsenal.id, season_id=season.id),
    ])
    db_session.flush()
    return {
        "season": season,
        "liverpool": liverpool,
        "arsenal": arsenal,
        "salah": salah,
        "saka": saka,
        "smith_a": smith_a,
        "smith_b": smith_b,
    }


def _provenance() -> ExtractionProvenance:
    return ExtractionProvenance(
        extractor_name="phase9.prompted_extractor",
        provider_name="mock",
        model_name="mock-deterministic-v1",
        template_id="phase9.combined",
        template_version="v1",
        prompt_hash="0" * 64,
        schema_version="phase9.extraction.v1",
        is_mock=True,
        requested_at=_utc(2025, 8, 15),
        completed_at=_utc(2025, 8, 15),
    )


# ---------------------------------------------------------------------------
# Provider-key normalization
# ---------------------------------------------------------------------------


class TestProviderKeyNormalization:
    def test_aliases_map_to_canonical(self):
        for alias in ("real_fpl", "fpl", "real_fpl_bootstrap", "live_intelligence"):
            assert canonical_provider_key(alias) == CANONICAL_FPL_ELEMENT_KEY

    def test_unknown_name_passes_through(self):
        assert canonical_provider_key("understat") == "understat"
        assert canonical_provider_key(None) == ""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestEntityResolver:
    def test_resolve_by_external_id_multiple_namespaces(self, db_session: Session):
        s = _seed_players(db_session)
        seed_fpl_external_id(
            db_session, provider="real_fpl", provider_player_id="100",
            canonical_player_id=s["salah"].id,
        )
        seed_fpl_external_id(
            db_session, provider="real_fpl_bootstrap", provider_player_id="200",
            canonical_player_id=s["saka"].id,
        )
        db_session.flush()

        resolve = build_entity_resolver(db_session)
        r1 = resolve("Salah", "Liverpool", external_id="real_fpl:100")
        assert r1.status is ResolutionStatus.RESOLVED_BY_EXTERNAL_ID
        assert r1.canonical_id == s["salah"].id

        r2 = resolve("Saka", "Arsenal", external_id="real_fpl_bootstrap:200")
        assert r2.status is ResolutionStatus.RESOLVED_BY_EXTERNAL_ID
        assert r2.canonical_id == s["saka"].id

    def test_provider_key_mismatch_tolerance(self, db_session: Session):
        """A real_fpl_bootstrap id must resolve even though seeded under real_fpl."""
        s = _seed_players(db_session)
        seed_fpl_external_id(
            db_session, provider="real_fpl", provider_player_id="555",
            canonical_player_id=s["salah"].id,
        )
        db_session.flush()
        resolve = build_entity_resolver(db_session)
        # The alias makes real_fpl_bootstrap resolve to the same canonical key.
        assert canonical_provider_key("real_fpl_bootstrap") == CANONICAL_FPL_ELEMENT_KEY
        r = resolve("Salah", "Liverpool", external_id="real_fpl_bootstrap:555")
        assert r.status is ResolutionStatus.RESOLVED_BY_EXTERNAL_ID
        assert r.canonical_id == s["salah"].id

    def test_resolve_by_name_and_team(self, db_session: Session):
        s = _seed_players(db_session)
        resolve = build_entity_resolver(db_session)
        r = resolve("Salah", "Liverpool", season_id=s["season"].id)
        assert r.status is ResolutionStatus.RESOLVED_BY_NAME_TEAM
        assert r.canonical_id == s["salah"].id

    def test_resolve_by_unique_name(self, db_session: Session):
        s = _seed_players(db_session)
        resolve = build_entity_resolver(db_session)
        r = resolve("Saka", None)
        assert r.status is ResolutionStatus.RESOLVED_BY_NAME_UNIQUE
        assert r.canonical_id == s["saka"].id

    def test_resolve_unique_name_without_season_active(self, db_session: Session):
        s = _seed_players(db_session)
        resolve = build_entity_resolver(db_session)
        # Two "Smith" players => ambiguous, even with team context only naming one.
        r = resolve("Smith", "Liverpool", season_id=s["season"].id)
        assert r.status is ResolutionStatus.RESOLVED_BY_NAME_TEAM
        assert r.canonical_id == s["smith_a"].id

    def test_ambiguous_player(self, db_session: Session):
        _seed_players(db_session)
        resolve = build_entity_resolver(db_session)
        r = resolve("Smith", None)
        assert r.status is ResolutionStatus.AMBIGUOUS_PLAYER
        assert r.canonical_id is None

    def test_unresolved_player(self, db_session: Session):
        _seed_players(db_session)
        resolve = build_entity_resolver(db_session)
        r = resolve("Nobody", None)
        assert r.status is ResolutionStatus.UNRESOLVED_PLAYER
        assert r.canonical_id is None

    def test_unresolved_team(self, db_session: Session):
        _seed_players(db_session)
        resolve = build_entity_resolver(db_session)
        r = resolve(None, "NonExistent FC")
        assert r.status is ResolutionStatus.UNRESOLVED_TEAM
        assert r.canonical_id is None


# ---------------------------------------------------------------------------
# Unresolved evidence persistence
# ---------------------------------------------------------------------------


def _availability_result(
    raw_item_id: int, player_name: str, team_name: str | None = None
) -> ExtractionResult:
    from fpl_intelligence.live_intelligence.extraction import AvailabilityEvidenceDraft

    draft = AvailabilityEvidenceDraft(
        player_name=player_name,
        team_name=team_name,
        evidence_type=str(EvidenceType.INJURY),
        status_mentioned=str(AvailabilityStatus.OUT),
        confidence=0.8,
        expected_absence_gameweeks=None,
        source_quote=f"{player_name} is ruled out.",
        reasoning="mock",
        raw_item_id=raw_item_id,
        published_at=_utc(2025, 8, 15),
        available_at=_utc(2025, 8, 15),
        ingested_at=_utc(2025, 8, 15),
        temporal_class="no_deadline_context",
        prompt_hash="0" * 64,
        prompt_template_id="phase9.combined",
        provider_name="mock",
        model_name="mock-deterministic-v1",
    )
    return ExtractionResult(
        raw_item_id=raw_item_id,
        status=ExtractionStatus.OK,
        provenance=_provenance(),
        availability=[draft],
    )


def _tactical_result(
    raw_item_id: int,
    *,
    team_name: str | None = None,
    player_name: str | None = None,
) -> ExtractionResult:
    from fpl_intelligence.live_intelligence.extraction import TacticalEvidenceDraft
    from fpl_intelligence.live_intelligence.models import TacticalDirection, TacticalEvidenceType

    draft = TacticalEvidenceDraft(
        evidence_type=str(TacticalEvidenceType.ROTATION_TENDENCY),
        team_name=team_name,
        player_name=player_name,
        value_text=None,
        numeric_value=None,
        direction=str(TacticalDirection.NEGATIVE),
        confidence=0.6,
        source_quote="rotate the squad",
        reasoning="mock",
        raw_item_id=raw_item_id,
        published_at=_utc(2025, 8, 15),
        available_at=_utc(2025, 8, 15),
        ingested_at=_utc(2025, 8, 15),
        temporal_class="no_deadline_context",
        prompt_hash="0" * 64,
        prompt_template_id="phase9.combined",
        provider_name="mock",
        model_name="mock-deterministic-v1",
    )
    return ExtractionResult(
        raw_item_id=raw_item_id,
        status=ExtractionStatus.OK,
        provenance=_provenance(),
        tactical=[draft],
    )


def _make_raw_item(db_session: Session, source_id: int) -> int:
    from fpl_intelligence.live_intelligence.models import LiveIntelligenceRawItem

    item = LiveIntelligenceRawItem(
        source_id=source_id,
        content_hash="a" * 64,
        raw_text="Salah is ruled out.",
        scraped_at=_utc(2025, 8, 15),
        available_at=_utc(2025, 8, 15),
        ingested_at=_utc(2025, 8, 15),
        temporal_class="no_deadline_context",
    )
    db_session.add(item)
    db_session.flush()
    return item.id


class TestUnresolvedPersistence:
    def test_unresolved_player_availability_persisted(self, db_session: Session):
        s = _seed_players(db_session)
        source_id = _register_source(db_session)
        raw_item_id = _make_raw_item(db_session, source_id)

        resolve = build_entity_resolver(db_session)
        result = _availability_result(raw_item_id, "Nobody", None)
        report = persist_extraction(
            db_session, result, season_id=s["season"].id,
            resolve_player=resolve, resolve_team=resolve,
        )
        db_session.flush()

        # Availability cannot be persisted without a player id.
        assert report.availability_persisted == 0
        assert report.unresolved_count == 1
        assert report.ambiguous_count == 0
        rows = list(db_session.scalars(select(UnresolvedLiveEvidence)))
        assert len(rows) == 1
        row = rows[0]
        assert row.raw_item_id == raw_item_id
        assert row.player_name == "Nobody"
        assert row.resolution_status == ResolutionStatus.UNRESOLVED_PLAYER
        assert row.extraction_run_id == report.extraction_run_id
        assert report.unresolved_evidence_ids == [row.id]

    def test_unresolved_team_tactical_persisted(self, db_session: Session):
        _seed_players(db_session)
        source_id = _register_source(db_session)
        raw_item_id = _make_raw_item(db_session, source_id)

        resolve = build_entity_resolver(db_session)
        result = _tactical_result(raw_item_id, team_name="NonExistent FC")
        report = persist_extraction(
            db_session, result, resolve_player=resolve, resolve_team=resolve
        )
        db_session.flush()

        assert report.tactical_persisted == 1
        assert report.unresolved_count == 1
        rows = list(db_session.scalars(select(UnresolvedLiveEvidence)))
        assert len(rows) == 1
        assert rows[0].team_name == "NonExistent FC"
        assert rows[0].resolution_status == ResolutionStatus.UNRESOLVED_TEAM

    def test_ambiguous_player_recorded(self, db_session: Session):
        s = _seed_players(db_session)
        source_id = _register_source(db_session)
        raw_item_id = _make_raw_item(db_session, source_id)

        resolve = build_entity_resolver(db_session)
        result = _availability_result(raw_item_id, "Smith", None)
        report = persist_extraction(
            db_session, result, season_id=s["season"].id,
            resolve_player=resolve, resolve_team=resolve,
        )
        db_session.flush()
        assert report.ambiguous_count == 1
        assert report.unresolved_count == 1
        row = db_session.get(UnresolvedLiveEvidence, report.unresolved_evidence_ids[0])
        assert row.resolution_status == ResolutionStatus.AMBIGUOUS_PLAYER

    def test_resolved_player_no_unresolved_row(self, db_session: Session):
        s = _seed_players(db_session)
        source_id = _register_source(db_session)
        raw_item_id = _make_raw_item(db_session, source_id)

        resolve = build_entity_resolver(db_session)
        result = _availability_result(raw_item_id, "Salah", "Liverpool")
        report = persist_extraction(
            db_session, result, season_id=s["season"].id,
            resolve_player=resolve, resolve_team=resolve,
        )
        db_session.flush()
        assert report.availability_persisted == 1
        assert report.resolved == 1
        assert report.unresolved_count == 0
        assert list(db_session.scalars(select(UnresolvedLiveEvidence))) == []


# ---------------------------------------------------------------------------
# End-to-end via ingest_raw_text
# ---------------------------------------------------------------------------


class TestIngestUnresolved:
    def test_raw_item_persisted_even_when_unresolved(self, db_session: Session):
        _seed_players(db_session)

        class _NoMatchProvider:
            @property
            def provider_name(self):
                return "mock"

            @property
            def model_name(self):
                return "mock"

            @property
            def is_mock(self):
                return True

            def complete(self, prompt):  # noqa: ANN001
                import json

                envelope = {
                    "schema_version": "phase9.extraction.v1",
                    "availability_evidence": [
                        {
                            "player_name": "Ghost Player",
                            "team_name": None,
                            "evidence_type": "injury",
                            "status_mentioned": "out",
                            "confidence": 0.8,
                            "expected_absence_gameweeks": None,
                            "source_quote": "Ghost Player is ruled out.",
                            "reasoning": "mock",
                        }
                    ],
                    "tactical_evidence": [],
                    "no_evidence_found": False,
                    "extraction_notes": "",
                }
                from fpl_intelligence.live_intelligence.extraction import LLMResponse

                return LLMResponse(
                    text=json.dumps(envelope),
                    provider_name="mock",
                    model_name="mock",
                    is_mock=True,
                )

        report = ingest_raw_text(
            db_session,
            source_id="press_conference_manual",
            text="Ghost Player is ruled out.",
            published_at=_utc(2025, 8, 15, 14),
            provider=_NoMatchProvider(),  # type: ignore[arg-type]
            season_code="2025-26",
            gameweek_number=None,
        )
        assert report.status is ManualIngestStatus.CREATED
        assert report.raw_item_id is not None
        assert report.availability_count == 0
        assert report.unresolved_count == 1
        assert report.unresolved_evidence_ids
        # The raw item survives and links to an unresolved evidence row.
        unresolved = db_session.get(UnresolvedLiveEvidence, report.unresolved_evidence_ids[0])
        assert unresolved.raw_item_id == report.raw_item_id

    def test_duplicate_content_skipped(self, db_session: Session):
        _seed_players(db_session)

        class _Provider:
            @property
            def provider_name(self):
                return "mock"

            @property
            def model_name(self):
                return "mock"

            @property
            def is_mock(self):
                return True

            def complete(self, prompt):  # noqa: ANN001
                import json

                from fpl_intelligence.live_intelligence.extraction import LLMResponse

                envelope = {
                    "schema_version": "phase9.extraction.v1",
                    "availability_evidence": [
                        {
                            "player_name": "Salah",
                            "team_name": "Liverpool",
                            "evidence_type": "injury",
                            "status_mentioned": "out",
                            "confidence": 0.8,
                            "expected_absence_gameweeks": None,
                            "source_quote": "Salah is ruled out for the next three weeks.",
                            "reasoning": "mock",
                        }
                    ],
                    "tactical_evidence": [],
                    "no_evidence_found": False,
                    "extraction_notes": "",
                }
                return LLMResponse(
                    text=json.dumps(envelope),
                    provider_name="mock",
                    model_name="mock",
                    is_mock=True,
                )

        text = "Salah is ruled out for the next three weeks."
        first = ingest_raw_text(
            db_session,
            source_id="press_conference_manual",
            text=text,
            published_at=_utc(2025, 8, 15, 14),
            provider=_Provider(),  # type: ignore[arg-type]
            season_code="2025-26",
        )
        assert first.status is ManualIngestStatus.CREATED
        second = ingest_raw_text(
            db_session,
            source_id="press_conference_manual",
            text=text,
            published_at=_utc(2025, 8, 15, 14),
            provider=_Provider(),  # type: ignore[arg-type]
            season_code="2025-26",
        )
        assert second.status is ManualIngestStatus.DUPLICATE
