"""Tests for temporal integrity.

Ensures historical data can be filtered by cutoff timestamp,
and that temporal fields are properly populated.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    FPLSnapshot,
    Gameweek,
    Player,
    PlayerTeamMembership,
    Season,
    Team,
)


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


class TestTemporalIntegrity:
    """Test temporal field handling."""

    def test_snapshot_filter_by_cutoff(self, db_session: Session) -> None:
        """Snapshots should be filterable by a cutoff timestamp."""
        season = Season(code="2024-25", display_name="2024/25")
        player = Player(first_name="Test", second_name="Player", web_name="T. Player", position_code=3)
        gw1 = Gameweek(season_id=1, provider_event_id=1, name="Gameweek 1")
        gw2 = Gameweek(season_id=1, provider_event_id=2, name="Gameweek 2")
        db_session.add_all([season, player, gw1, gw2])
        db_session.flush()

        # Snapshot before Gameweek 2 deadline
        snap_before = FPLSnapshot(
            player_id=player.id,
            season_id=season.id,
            gameweek_id=gw1.id,
            event_time=datetime(2024, 8, 16, 12, 0, tzinfo=UTC),
            published_at=datetime(2024, 8, 16, 12, 0, tzinfo=UTC),
            ingested_at=datetime.now(UTC),
            price=8.0,
        )
        db_session.add(snap_before)
        db_session.flush()

        # Snapshot after Gameweek 2 deadline
        snap_after = FPLSnapshot(
            player_id=player.id,
            season_id=season.id,
            gameweek_id=gw2.id,
            event_time=datetime(2024, 8, 23, 12, 0, tzinfo=UTC),
            published_at=datetime(2024, 8, 23, 12, 0, tzinfo=UTC),
            ingested_at=datetime.now(UTC),
            price=8.5,
        )
        db_session.add(snap_after)
        db_session.commit()

        # Filter by cutoff before Gameweek 2
        cutoff = datetime(2024, 8, 20, 0, 0, tzinfo=UTC)
        available_before = db_session.query(FPLSnapshot).filter(
            FPLSnapshot.event_time <= cutoff,
        ).all()
        assert len(available_before) == 1
        assert available_before[0].price == 8.0

        # All snapshots
        all_snapshots = db_session.query(FPLSnapshot).all()
        assert len(all_snapshots) == 2

    def test_player_team_membership_temporal(self, db_session: Session) -> None:
        """Player team membership should be filterable by time."""
        season = Season(code="2024-25", display_name="2024/25")
        team_a = Team(name="Team A", short_name="TMA")
        team_b = Team(name="Team B", short_name="TMB")
        player = Player(first_name="Test", second_name="Player", web_name="T. Player", position_code=3)
        db_session.add_all([season, team_a, team_b, player])
        db_session.flush()

        # Membership A (first half)
        db_session.add(PlayerTeamMembership(
            player_id=player.id,
            team_id=team_a.id,
            season_id=season.id,
            valid_from=datetime(2024, 8, 1, tzinfo=UTC),
            valid_to=datetime(2025, 1, 1, tzinfo=UTC),
        ))

        # Membership B (second half)
        db_session.add(PlayerTeamMembership(
            player_id=player.id,
            team_id=team_b.id,
            season_id=season.id,
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        ))
        db_session.commit()

        # Query membership at a specific time
        mid_season = datetime(2024, 12, 1, tzinfo=UTC)
        membership_at = db_session.query(PlayerTeamMembership).filter(
            PlayerTeamMembership.player_id == player.id,
            PlayerTeamMembership.valid_from <= mid_season,
            (PlayerTeamMembership.valid_to.is_(None) | (PlayerTeamMembership.valid_to >= mid_season)),
        ).first()
        assert membership_at is not None
        assert membership_at.team_id == team_a.id

        # Query membership after transfer
        after_transfer = datetime(2025, 2, 1, tzinfo=UTC)
        membership_after = db_session.query(PlayerTeamMembership).filter(
            PlayerTeamMembership.player_id == player.id,
            PlayerTeamMembership.valid_from <= after_transfer,
            (PlayerTeamMembership.valid_to.is_(None) | (PlayerTeamMembership.valid_to >= after_transfer)),
        ).first()
        assert membership_after is not None
        assert membership_after.team_id == team_b.id

    def test_temporal_fields_present(self, db_session: Session) -> None:
        """Verify that temporal fields exist on time-varying records."""
        # FPLSnapshot has event_time, published_at, ingested_at
        columns = [c.name for c in FPLSnapshot.__table__.columns]
        assert "event_time" in columns
        assert "published_at" in columns
        assert "ingested_at" in columns

        # PlayerTeamMembership has valid_from, valid_to
        columns_ptm = [c.name for c in PlayerTeamMembership.__table__.columns]
        assert "valid_from" in columns_ptm
        assert "valid_to" in columns_ptm