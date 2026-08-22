"""Phase 9.2 unit tests — Source Registry, Raw Item Ledger, deduplication, manual ingestion."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.availability.models import AvailabilityEvidence
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import Gameweek, Player, Season
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider
from fpl_intelligence.live_intelligence.raw_item_ledger import (
    ManualIngestStatus,
    RawItem,
    RawItemDeduplicator,
    ingest_raw_text,
)
from fpl_intelligence.live_intelligence.source_registry import (
    ReliabilityTier,
    SourceRegistry,
    SourceType,
    map_source_type_to_live,
    map_tier_to_reliability,
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


def _utc(year, month, day, hour=0, minute=0, tzinfo=True):
    from datetime import UTC, datetime

    dt = datetime(year, month, day, hour, minute)
    return dt.replace(tzinfo=UTC) if tzinfo else dt


# ---------------------------------------------------------------------------
# Source Registry — tier classification
# ---------------------------------------------------------------------------


class TestSourceRegistryTiers:
    def test_classify_tier_by_source_type(self):
        reg = SourceRegistry()
        T = SourceType
        R = ReliabilityTier
        expected = {
            T.OFFICIAL_API: R.TIER_0_OFFICIAL_STRUCTURED,
            T.PRESS_CONFERENCE: R.TIER_1_OFFICIAL_UNSTRUCTURED,
            T.CLUB_SITE: R.TIER_1_OFFICIAL_UNSTRUCTURED,
            T.JOURNALIST: R.TIER_2_RELIABLE_JOURNALIST,
            T.RSS: R.TIER_3_AGGREGATOR,
            T.AGGREGATOR: R.TIER_3_AGGREGATOR,
            T.SOCIAL: R.TIER_4_SOCIAL_UNVERIFIED,
            T.MANUAL: R.TIER_4_SOCIAL_UNVERIFIED,
        }
        for source_type, tier in expected.items():
            assert reg.classify_tier(source_type) is tier

    def test_tier_rank_ordering(self):
        ranks = [
            t.rank
            for t in (
                ReliabilityTier.TIER_0_OFFICIAL_STRUCTURED,
                ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED,
                ReliabilityTier.TIER_2_RELIABLE_JOURNALIST,
                ReliabilityTier.TIER_3_AGGREGATOR,
                ReliabilityTier.TIER_4_SOCIAL_UNVERIFIED,
            )
        ]
        assert ranks == sorted(ranks)
        assert ReliabilityTier.TIER_0_OFFICIAL_STRUCTURED < ReliabilityTier.TIER_4_SOCIAL_UNVERIFIED

    def test_official_club_promotes_untrusted_source(self):
        reg = SourceRegistry()
        # A club speaking for itself is official even via a loose channel.
        assert reg.classify_tier(SourceType.SOCIAL, is_official_club=True) is (
            ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED
        )

    def test_tier_flags(self):
        assert ReliabilityTier.TIER_0_OFFICIAL_STRUCTURED.is_official
        assert ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED.is_official
        assert not ReliabilityTier.TIER_4_SOCIAL_UNVERIFIED.is_official
        assert ReliabilityTier.TIER_0_OFFICIAL_STRUCTURED.is_structured
        assert ReliabilityTier.TIER_4_SOCIAL_UNVERIFIED.is_unverified

    def test_register_overrides_tier(self):
        reg = SourceRegistry()
        reg.register(
            "custom",
            SourceType.MANUAL,
            reliability_tier=ReliabilityTier.TIER_2_RELIABLE_JOURNALIST,
        )
        assert reg.tier_for("custom") is ReliabilityTier.TIER_2_RELIABLE_JOURNALIST
        assert reg.get("custom").is_official is False

    def test_source_type_to_live_mapping(self):
        assert map_source_type_to_live(SourceType.OFFICIAL_API).value == "fpl_official"
        assert map_source_type_to_live(SourceType.PRESS_CONFERENCE).value == "press_conference"
        assert map_source_type_to_live(SourceType.CLUB_SITE).value == "club_official"
        assert map_source_type_to_live(SourceType.MANUAL).value == "other"

    def test_tier_to_reliability_mapping(self):
        assert (
            map_tier_to_reliability(ReliabilityTier.TIER_0_OFFICIAL_STRUCTURED).value == "official"
        )
        assert (
            map_tier_to_reliability(ReliabilityTier.TIER_2_RELIABLE_JOURNALIST).value
            == "verified_journalist"
        )
        assert (
            map_tier_to_reliability(ReliabilityTier.TIER_4_SOCIAL_UNVERIFIED).value == "unverified"
        )

    def test_ensure_source_persists_tier_to_db(self, db_session: Session):
        reg = SourceRegistry()
        source = reg.ensure_source(
            db_session,
            "press_conference_manual",
            source_type=SourceType.PRESS_CONFERENCE,
        )
        db_session.commit()
        # Round-trips: a fresh registry recovers the tier from the DB notes marker.
        fresh = SourceRegistry()
        loaded = fresh.load_from_db(db_session)
        assert loaded == 1
        assert (
            fresh.tier_for("press_conference_manual")
            is ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED
        )
        assert source.environment == "real"


# ---------------------------------------------------------------------------
# RawItem — temporal validation
# ---------------------------------------------------------------------------


class TestRawItemTemporal:
    def test_create_computes_hash_and_defaults_available_at(self):
        raw = RawItem.create(
            source_id="press_conference_manual",
            title="PC",
            content_text="Salah is ruled out.",
            published_at=_utc(2025, 8, 15, 14),
            scraped_at=_utc(2025, 8, 15, 14, 5),
            ingested_at=_utc(2025, 8, 15, 14, 10),
        )
        assert raw.content_hash
        assert len(raw.content_hash) == 64
        # available_at defaults to published_at.
        assert raw.available_at == raw.published_at

    def test_whitespace_normalization_in_hash(self):
        a = RawItem.create(
            source_id="s",
            title="t",
            content_text="Salah   is   ruled   out.",
            published_at=_utc(2025, 8, 15),
            scraped_at=_utc(2025, 8, 15),
            ingested_at=_utc(2025, 8, 15),
        )
        b = RawItem.create(
            source_id="s",
            title="t",
            content_text="Salah is ruled out.",
            published_at=_utc(2025, 8, 15),
            scraped_at=_utc(2025, 8, 15),
            ingested_at=_utc(2025, 8, 15),
        )
        assert a.content_hash == b.content_hash

    def test_available_at_must_not_precede_published_at(self):
        with pytest.raises(ValidationError):
            RawItem.create(
                source_id="s",
                title="t",
                content_text="x",
                published_at=_utc(2025, 8, 15, 14, 10),
                scraped_at=_utc(2025, 8, 15, 14, 5),
                ingested_at=_utc(2025, 8, 15, 14, 5),
                available_at=_utc(2025, 8, 15, 14, 0),
            )

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValidationError):
            RawItem(
                source_id="s",
                title="t",
                content_text="x",
                content_hash="0" * 64,
                published_at=_utc(2025, 8, 15, 14, 10, tzinfo=False),
                scraped_at=_utc(2025, 8, 15, 14, 10, tzinfo=False),
                available_at=_utc(2025, 8, 15, 14, 10, tzinfo=False),
                ingested_at=_utc(2025, 8, 15, 14, 10, tzinfo=False),
                temporal_class="no_deadline_context",
            )


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


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


class TestDeduplication:
    def test_hash_and_skip_duplicate(self, db_session: Session):
        source_id = _register_source(db_session)
        dedup = RawItemDeduplicator(db_session)

        text = "Salah is ruled out for the next three weeks."
        from fpl_intelligence.live_intelligence.temporal_ledger import content_hash

        digest = content_hash(text)
        assert not dedup.is_duplicate(source_id, digest)

        # Simulate a persisted item via the ledger.
        from fpl_intelligence.live_intelligence.models import LiveIntelligenceRawItem

        item = LiveIntelligenceRawItem(
            source_id=source_id,
            content_hash=digest,
            raw_text=text,
            scraped_at=_utc(2025, 8, 15, 14),
            available_at=_utc(2025, 8, 15, 14),
            ingested_at=_utc(2025, 8, 15, 14, 5),
            temporal_class="no_deadline_context",
        )
        db_session.add(item)
        db_session.flush()

        assert dedup.is_duplicate(source_id, digest)

    def test_in_memory_cache_fronts_db(self, db_session: Session):
        source_id = _register_source(db_session)
        dedup = RawItemDeduplicator(db_session, use_cache=True)
        digest = "a" * 64
        assert dedup.is_duplicate(source_id, digest) is False
        dedup.remember(source_id, digest)
        assert dedup.is_duplicate(source_id, digest) is True

    def test_manual_ingest_skips_duplicate(
        self,
        db_session: Session,
    ):
        season = Season(code="2025-26", display_name="2025/26")
        db_session.add(season)
        db_session.add(Player(first_name="Mohamed", second_name="Salah", web_name="Salah"))
        db_session.flush()

        text = "Salah is injured."
        provider = MockLLMProvider(player_names=["Salah"])

        first = ingest_raw_text(
            db_session,
            source_id="press_conference_manual",
            text=text,
            published_at=_utc(2025, 8, 15, 14),
            provider=provider,
            season_code="2025-26",
            gameweek_number=None,
        )
        assert first.status is ManualIngestStatus.CREATED
        assert first.availability_count >= 1

        # Second submission of identical text from the same source is a no-op.
        second = ingest_raw_text(
            db_session,
            source_id="press_conference_manual",
            text=text,
            published_at=_utc(2025, 8, 15, 14),
            provider=provider,
            season_code="2025-26",
        )
        assert second.status is ManualIngestStatus.DUPLICATE
        assert second.duplicate is True
        assert second.extraction_run_id is None


# ---------------------------------------------------------------------------
# Manual ingestion + extraction bridge
# ---------------------------------------------------------------------------


class TestManualIngestion:
    def _seed(self, db_session: Session):
        season = Season(code="2025-26", display_name="2025/26")
        db_session.add(season)
        db_session.flush()
        gw = Gameweek(
            season_id=season.id,
            provider_event_id=3,
            name="Gameweek 3",
            deadline_time=_utc(2025, 8, 20, 18),
            status="scheduled",
        )
        db_session.add(gw)
        db_session.add(Player(first_name="Mohamed", second_name="Salah", web_name="Salah"))
        db_session.flush()
        return season, gw

    def test_ingest_extracts_and_persists_evidence(
        self,
        db_session: Session,
    ):
        season, gw = self._seed(db_session)
        published = _utc(2025, 8, 15, 14, 0)
        provider = MockLLMProvider(player_names=["Salah"])

        report = ingest_raw_text(
            db_session,
            source_id="press_conference_manual",
            text="Salah is injured.",
            published_at=published,
            url="https://example.com/pc",
            external_id="pc-123",
            provider=provider,
            season_code="2025-26",
            gameweek_number=3,
        )

        assert report.status is ManualIngestStatus.CREATED
        assert report.raw_item_id is not None
        assert report.extraction_run_id is not None
        assert report.availability_count >= 1
        assert report.tactical_count >= 0
        assert report.availability_evidence_ids

        # Extraction bridge: the availability evidence inherits the RawItem's
        # published_at/available_at, proving provenance flowed through.
        evidence = db_session.get(AvailabilityEvidence, report.availability_evidence_ids[0])
        assert evidence is not None
        # SQLite stores tz-aware instants as naive UTC; compare in naive form.
        assert evidence.valid_from == published.replace(tzinfo=None)

    def test_ingest_rejects_inconsistent_temporal(self, db_session: Session):
        self._seed(db_session)
        report = ingest_raw_text(
            db_session,
            source_id="press_conference_manual",
            text="Salah is ruled out.",
            # published_at in the future relative to the (default) ingestion clock.
            published_at=_utc(2099, 1, 1, 0, 0),
        )
        assert report.status is ManualIngestStatus.REJECTED
        assert report.error


# ---------------------------------------------------------------------------
# Manual ingestion SCRIPT (mock file + MockLLMProvider)
# ---------------------------------------------------------------------------


class TestManualIngestScript:
    def test_script_ingests_mock_file(self, tmp_path, capsys):
        import importlib.util
        import os

        script_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "scripts", "manual_ingest_raw_text.py"
        )
        spec = importlib.util.spec_from_file_location("manual_ingest_raw_text", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        txt = tmp_path / "transcript.txt"
        txt.write_text("Haaland is ruled out with a knock.", encoding="utf-8")
        db_path = tmp_path / "ingest.db"

        rc = module.main(
            [
                "--source-id",
                "press_conference_manual",
                "--file",
                str(txt),
                "--published-at",
                "2025-08-15T14:00:00Z",
                "--provider",
                "mock",
                "--db",
                str(db_path),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "MANUAL INGESTION SUMMARY" in out
        assert "content_hash" in out

    def test_script_logs_duplicate_and_exits_cleanly(self, tmp_path, capsys):
        import importlib.util
        import os

        script_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "scripts", "manual_ingest_raw_text.py"
        )
        spec = importlib.util.spec_from_file_location("manual_ingest_raw_text", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        txt = tmp_path / "transcript.txt"
        txt.write_text("Haaland is ruled out with a knock.", encoding="utf-8")
        db_path = tmp_path / "ingest.db"

        # First run: created.
        rc1 = module.main(
            [
                "--source-id",
                "press_conference_manual",
                "--file",
                str(txt),
                "--published-at",
                "2025-08-15T14:00:00Z",
                "--provider",
                "mock",
                "--db",
                str(db_path),
            ]
        )
        assert rc1 == 0

        # Second run on the same db: duplicate, clean exit (0).
        rc2 = module.main(
            [
                "--source-id",
                "press_conference_manual",
                "--file",
                str(txt),
                "--published-at",
                "2025-08-15T14:00:00Z",
                "--provider",
                "mock",
                "--db",
                str(db_path),
            ]
        )
        assert rc2 == 0
        out = capsys.readouterr().out
        assert "Duplicate content detected, skipping extraction" in out
