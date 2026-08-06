"""Shared test fixtures for the FPL Intelligence Engine."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    Fixture,
    FPLSnapshot,
    Gameweek,
    Player,
    PlayerExternalId,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
    Season,
    Team,
    TeamExternalId,
)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Create an in-memory SQLite database with all tables."""
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()

    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def cutoff_time() -> datetime:
    """Return a test cutoff time."""
    return datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def populated_db(db_session: Session) -> Session:
    """Create a database with test data for Phase 3 tests."""
    db = db_session

    season = Season(
        code="2025-26",
        display_name="2025/26",
        start_date=datetime(2025, 8, 1, tzinfo=UTC),
        end_date=datetime(2026, 5, 31, tzinfo=UTC),
        competition="Premier League",
    )
    db.add(season)
    db.flush()

    teams = []
    team_names = ["Arsenal", "Chelsea", "Liverpool", "Manchester City"]
    for i, name in enumerate(team_names):
        team = Team(name=name, short_name=name[:3].upper())
        db.add(team)
        db.flush()
        db.add(TeamExternalId(team_id=team.id, provider="mock", provider_team_id=f"mock_team_{i}"))
        teams.append(team)
    db.flush()

    players = []
    player_data = [
        ("Alisson", "Becker", "Alisson", 1, 0),
        ("William", "Saliba", "Saliba", 2, 0),
        ("Martin", "Odegaard", "Odegaard", 3, 0),
        ("Erling", "Haaland", "Haaland", 4, 3),
    ]
    for first, second, web, pos, team_idx in player_data:
        player = Player(first_name=first, second_name=second, web_name=web, position_code=pos)
        db.add(player)
        db.flush()
        db.add(PlayerExternalId(player_id=player.id, provider="mock", provider_player_id=f"mock_player_{web.lower()}"))
        db.add(PlayerTeamMembership(player_id=player.id, team_id=teams[team_idx].id, season_id=season.id, valid_from=season.start_date))
        players.append(player)
    db.flush()

    gameweeks = []
    for gw_num in range(1, 4):
        gw = Gameweek(
            season_id=season.id,
            provider_event_id=gw_num,
            name=f"Gameweek {gw_num}",
            deadline_time=datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=(gw_num - 1) * 7),
            start_time=datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=(gw_num - 1) * 7, hours=2),
            end_time=datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=(gw_num - 1) * 7 + 2),
            status="scheduled",
        )
        db.add(gw)
        gameweeks.append(gw)
    db.flush()

    fixtures = []
    fixture_idx = 0
    for gw_idx, gw in enumerate(gameweeks):
        f1 = Fixture(
            season_id=season.id, provider_fixture_id=fixture_idx + 1, gameweek_id=gw.id,
            kickoff_time=datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=gw_idx * 7, hours=3),
            home_team_id=teams[0].id, away_team_id=teams[1].id,
            home_score=2 if gw_idx < 2 else None, away_score=1 if gw_idx < 2 else None,
            status="completed" if gw_idx < 2 else "scheduled", postponed=False,
        )
        db.add(f1)
        fixtures.append(f1)
        fixture_idx += 1

        f2 = Fixture(
            season_id=season.id, provider_fixture_id=fixture_idx + 1, gameweek_id=gw.id,
            kickoff_time=datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=gw_idx * 7, hours=5),
            home_team_id=teams[2].id, away_team_id=teams[3].id,
            home_score=1 if gw_idx < 2 else None, away_score=3 if gw_idx < 2 else None,
            status="completed" if gw_idx < 2 else "scheduled", postponed=False,
        )
        db.add(f2)
        fixtures.append(f2)
        fixture_idx += 1
    db.flush()

    for gw_idx in range(2):
        gw = gameweeks[gw_idx]
        gw_time = datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=gw_idx * 7)
        for player_idx, player in enumerate(players):
            team_idx = player_data[player_idx][4]
            team = teams[team_idx]
            db.add(PlayerGameweekPerformance(
                player_id=player.id, gameweek_id=gw.id, season_id=season.id, team_id=team.id,
                minutes=90 if player_idx < 3 else 80,
                goals_scored=1 if player_idx == 3 else 0,
                assists=1 if player_idx == 2 else 0,
                clean_sheets=1 if player_idx in (0, 1) else 0,
                goals_conceded=0 if player_idx in (0, 1) else 2,
                own_goals=0, penalties_saved=0, penalties_missed=0,
                yellow_cards=0, red_cards=0,
                saves=3 if player_idx == 0 else 0,
                bonus=2 if player_idx == 3 else 0,
                bps=20 + player_idx * 5,
                influence=50.0 - player_idx * 5, creativity=30.0 - player_idx * 3,
                threat=40.0 - player_idx * 4, ict_index=12.0 - player_idx,
                expected_goals=0.5 if player_idx == 3 else 0.0,
                expected_assists=0.3 if player_idx == 2 else 0.0,
                expected_goal_involvements=0.8 if player_idx >= 2 else 0.0,
                expected_goals_conceded=0.5 if player_idx < 2 else 1.5,
                total_points=10 + player_idx * 2 - gw_idx,
                value=100 - player_idx * 10,
                transfers_balance=1000 - player_idx * 100,
                selected=500000 - player_idx * 50000,
                transfers_in=10000 - player_idx * 1000,
                transfers_out=5000 - player_idx * 500,
                loaned_in=0, loaned_out=0,
                price=8.0 - player_idx * 0.5,
                cost_change_event=0, cost_change_start=0,
                price_change=0.1, price_start=7.5,
                form=3.5 + player_idx * 0.5,
                form_rank=player_idx + 1,
                points_per_game=4.0 + player_idx * 0.5,
                selected_by_percent=20.0 - player_idx * 3,
                selected_rank=player_idx + 1,
                ep_this=3.0, ep_next=3.5,
                ingested_at=gw_time + timedelta(hours=2),
                available_at=gw_time + timedelta(hours=2),
            ))
    db.flush()

    for gw_idx in range(2):
        gw = gameweeks[gw_idx]
        snap_time = datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=gw_idx * 7, hours=-1)
        for player_idx, player in enumerate(players):
            db.add(FPLSnapshot(
                player_id=player.id, season_id=season.id, gameweek_id=gw.id,
                event_time=snap_time, published_at=snap_time, available_at=snap_time,
                ingested_at=snap_time + timedelta(minutes=5),
                source_last_modified_at=snap_time,
                price=8.0 - player_idx * 0.5 + gw_idx * 0.1,
                selected_by_percent=20.0 - player_idx * 3 - gw_idx * 0.5,
                transfers_in_event=10000 - player_idx * 1000 - gw_idx * 500,
                transfers_out_event=5000 - player_idx * 500 + gw_idx * 200,
                transfers_in_season=10000 - player_idx * 1000,
                transfers_out_season=5000 - player_idx * 500,
                total_points=10 + player_idx * 2 - gw_idx,
                form=3.5 + player_idx * 0.5 + gw_idx * 0.1,
                points_per_game=4.0 + player_idx * 0.5,
                form_rank=player_idx + 1,
                points_per_game_rank=player_idx + 1,
                selected_rank=player_idx + 1,
                ep_this=3.0, ep_next=3.5,
            ))
    db.commit()

    return db