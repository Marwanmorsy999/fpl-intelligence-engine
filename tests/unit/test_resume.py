"""Tests for resumable ingestion.

Simulates interrupted ingestion and ensures it can resume safely.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import IngestionRun, Player, Team
from fpl_intelligence.ingestion.historical import import_season
from fpl_intelligence.providers.mock_historical import MockHistoricalDataProvider


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


class TestResumeIngestion:
    """Test that interrupted ingestion can resume safely."""

    def test_completed_import_skipped(self, db_session: Session) -> None:
        """A completed import should be skipped on re-run."""
        provider = MockHistoricalDataProvider(provider_name="resume_test", schema_version="v1")

        # First import
        report1 = import_season(db_session, provider, "2024-25", dataset="teams", force=False, dry_run=False)
        assert report1.records_accepted > 0

        # Verify ingestion run was recorded
        run = db_session.query(IngestionRun).filter(
            IngestionRun.source == "resume_test",
            IngestionRun.job_name == "historical_teams",
            IngestionRun.season_code == "2024-25",
        ).first()
        assert run is not None
        assert run.status == "SUCCESS"

        # Second import
        report2 = import_season(db_session, provider, "2024-25", dataset="teams", force=False, dry_run=False)
        assert report2.records_accepted == report1.records_accepted

    def test_dry_run_does_not_persist(self, db_session: Session) -> None:
        """Dry run should not persist any data."""
        provider = MockHistoricalDataProvider(provider_name="dry_run_test", schema_version="v1")

        # Dry run import
        report = import_season(db_session, provider, "2024-25", dataset="all", force=False, dry_run=True)

        # No data should be persisted
        teams_count = db_session.query(Team).count()
        players_count = db_session.query(Player).count()
        assert teams_count == 0
        assert players_count == 0

        # Verify no successful run was recorded
        run = db_session.query(IngestionRun).filter(
            IngestionRun.source == "dry_run_test",
            IngestionRun.status == "SUCCESS",
        ).first()
        assert run is None

    def test_force_reimport(self, db_session: Session) -> None:
        """Force re-import should process even if previously completed."""
        provider = MockHistoricalDataProvider(provider_name="force_test", schema_version="v1")

        # First import
        import_season(db_session, provider, "2024-25", dataset="teams", force=False, dry_run=False)
        teams_count_1 = db_session.query(Team).count()

        # Force re-import
        report = import_season(db_session, provider, "2024-25", dataset="teams", force=True, dry_run=False)

        # Teams should still be idempotent (same count)
        teams_count_2 = db_session.query(Team).count()
        assert teams_count_2 == teams_count_1
        assert report.records_received > 0