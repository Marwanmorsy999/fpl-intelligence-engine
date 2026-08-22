"""Tests for FPL snapshot preservation.

Ensures snapshots are properly saved, previous snapshots are preserved,
and accidental overwrites are prevented.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import FPLSnapshot, Gameweek, Player, Season


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


@pytest.fixture
def season(db_session: Session) -> Season:
    s = Season(code="2024-25", display_name="2024/25")
    db_session.add(s)
    db_session.flush()
    return s


@pytest.fixture
def player(db_session: Session) -> Player:
    p = Player(first_name="Test", second_name="Player", web_name="T. Player", position_code=3)
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture
def gameweek(db_session: Session, season: Season) -> Gameweek:
    gw = Gameweek(season_id=season.id, provider_event_id=1, name="Gameweek 1")
    db_session.add(gw)
    db_session.flush()
    return gw


class TestSnapshotInsertion:
    """Test inserting new snapshots."""

    def test_insert_new_snapshot(
        self, db_session: Session, player: Player, season: Season, gameweek: Gameweek
    ) -> None:
        snapshot = FPLSnapshot(
            player_id=player.id,
            season_id=season.id,
            gameweek_id=gameweek.id,
            event_time=datetime(2024, 8, 16, 18, 0, tzinfo=UTC),
            published_at=datetime(2024, 8, 16, 18, 0, tzinfo=UTC),
            ingested_at=datetime.now(UTC),
            price=8.5,
            selected_by_percent=25.0,
            transfers_in_event=10000,
            transfers_out_event=5000,
            total_points=30,
            form=4.5,
            points_per_game=5.0,
        )
        db_session.add(snapshot)
        db_session.commit()

        saved = db_session.query(FPLSnapshot).first()
        assert saved is not None
        assert saved.price == 8.5
        assert saved.selected_by_percent == 25.0
        assert saved.total_points == 30
        assert saved.player_id == player.id
        assert saved.gameweek_id == gameweek.id


class TestSnapshotPreservation:
    """Test that previous snapshots are preserved."""

    def test_preserve_previous_snapshot(
        self, db_session: Session, player: Player, season: Season, gameweek: Gameweek
    ) -> None:
        # First snapshot
        snap1 = FPLSnapshot(
            player_id=player.id,
            season_id=season.id,
            gameweek_id=gameweek.id,
            event_time=datetime(2024, 8, 16, 12, 0, tzinfo=UTC),
            published_at=datetime(2024, 8, 16, 12, 0, tzinfo=UTC),
            ingested_at=datetime.now(UTC),
            price=8.0,
            selected_by_percent=20.0,
            total_points=15,
        )
        db_session.add(snap1)
        db_session.commit()

        # Second snapshot (updated price)
        snap2 = FPLSnapshot(
            player_id=player.id,
            season_id=season.id,
            gameweek_id=gameweek.id,
            event_time=datetime(2024, 8, 17, 12, 0, tzinfo=UTC),
            published_at=datetime(2024, 8, 17, 12, 0, tzinfo=UTC),
            ingested_at=datetime.now(UTC),
            price=8.5,
            selected_by_percent=25.0,
            total_points=15,
        )
        db_session.add(snap2)
        db_session.commit()

        # Both snapshots should exist
        all_snapshots = db_session.query(FPLSnapshot).all()
        assert len(all_snapshots) == 2

        # First snapshot should still have the original price
        assert all_snapshots[0].price == 8.0  # First snapshot preserved
        assert all_snapshots[1].price == 8.5  # Second snapshot has new price


class TestSnapshotOverwritePrevention:
    """Test that accidental overwrites are prevented."""

    def test_unique_constraint_prevents_exact_duplicate(
        self, db_session: Session, player: Player, season: Season, gameweek: Gameweek
    ) -> None:
        event_time = datetime(2024, 8, 16, 18, 0, tzinfo=UTC)
        snap1 = FPLSnapshot(
            player_id=player.id,
            season_id=season.id,
            gameweek_id=gameweek.id,
            event_time=event_time,
            published_at=event_time,
            ingested_at=datetime.now(UTC),
            price=8.0,
        )
        db_session.add(snap1)
        db_session.commit()

        # Inserting the same unique key should either raise or be handled
        snap2 = FPLSnapshot(
            player_id=player.id,
            season_id=season.id,
            gameweek_id=gameweek.id,
            event_time=event_time,
            published_at=event_time,
            ingested_at=datetime.now(UTC),
            price=8.5,
        )
        db_session.add(snap2)

        # The unique constraint (player_id, gameweek_id, event_time) should prevent duplicates
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        # Verify original snapshot is unchanged
        saved = db_session.query(FPLSnapshot).first()
        assert saved is not None
        assert saved.price == 8.0
