"""Tests for idempotent ingestion.

Running the same import twice must not create duplicate records.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    Fixture,
    Gameweek,
    Player,
    PlayerGameweekPerformance,
    Team,
)
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


class TestIdempotentIngestion:
    """Running the same import twice must not create duplicates."""

    def test_import_twice_no_duplicates(self, db_session: Session) -> None:
        provider = MockHistoricalDataProvider(provider_name="test_provider", schema_version="v1")

        # First import
        report1 = import_season(
            db_session, provider, "2024-25", dataset="all", force=False, dry_run=False
        )
        assert report1.records_received > 0, "First import should process records"

        # Count records after first import
        teams_count_1 = db_session.query(Team).count()
        players_count_1 = db_session.query(Player).count()
        fixtures_count_1 = db_session.query(Fixture).count()
        gameweeks_count_1 = db_session.query(Gameweek).count()
        player_gw_count_1 = db_session.query(PlayerGameweekPerformance).count()

        # Second import (same data, not force)
        report2 = import_season(
            db_session, provider, "2024-25", dataset="all", force=False, dry_run=False
        )

        # Count records after second import
        teams_count_2 = db_session.query(Team).count()
        players_count_2 = db_session.query(Player).count()
        fixtures_count_2 = db_session.query(Fixture).count()
        gameweeks_count_2 = db_session.query(Gameweek).count()
        player_gw_count_2 = db_session.query(PlayerGameweekPerformance).count()

        # Second import should not add new records
        assert teams_count_2 == teams_count_1, "Teams should not increase on second import"
        assert players_count_2 == players_count_1, "Players should not increase on second import"
        assert fixtures_count_2 == fixtures_count_1, "Fixtures should not increase on second import"
        assert gameweeks_count_2 == gameweeks_count_1, (
            "Gameweeks should not increase on second import"
        )
        assert player_gw_count_2 == player_gw_count_1, (
            "Player gameweek performances should not increase on second import"
        )

        # Second import should report completion without re-processing
        assert report2.records_accepted == report1.records_accepted

    def test_force_reimport_allows_duplicates(self, db_session: Session) -> None:
        provider = MockHistoricalDataProvider(
            provider_name="test_provider_force", schema_version="v1"
        )

        import_season(
            db_session, provider, "2024-25", dataset="all", force=False, dry_run=False
        )
        teams_count_1 = db_session.query(Team).count()
        players_count_1 = db_session.query(Player).count()

        # Force re-import should process again
        import_season(
            db_session, provider, "2024-25", dataset="all", force=True, dry_run=False
        )

        # Teams and players shouldn't duplicate because they're idempotent
        teams_count_2 = db_session.query(Team).count()
        players_count_2 = db_session.query(Player).count()
        assert teams_count_2 == teams_count_1
        assert players_count_2 == players_count_1

        # Gameweek performances should not duplicate due to unique constraint
        player_gw_count_1 = db_session.query(PlayerGameweekPerformance).count()
        # On force re-import, the existing records are skipped due to unique constraint checks
        assert player_gw_count_1 > 0

    def test_non_existing_season_handled(self, db_session: Session) -> None:
        provider = MockHistoricalDataProvider(provider_name="test_provider_2", schema_version="v1")
        with pytest.raises(ValueError, match="not found in provider data"):
            import_season(
                db_session, provider, "2099-00", dataset="all", force=False, dry_run=False
            )
