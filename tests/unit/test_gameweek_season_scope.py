from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    Fixture,
    Gameweek,
    Player,
    PlayerTeamMembership,
    Season,
    Team,
)
from fpl_intelligence.prediction.live_provider import (
    LivePredictionProvider,
    _fixtures_for_gameweek,
    _resolve_gameweek_id,
)
from fpl_intelligence.sync.service import _get_or_create_gameweek


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def test_resolver_prefers_latest_season():
    engine = make_db()
    try:
        with Session(engine) as db:
            old = Season(code="2025-26", display_name="2025/26")
            cur = Season(code="2026-27", display_name="2026/27")
            db.add_all([old, cur])
            db.flush()
            a = Gameweek(season_id=old.id, provider_event_id=2, name="GW2")
            b = Gameweek(season_id=cur.id, provider_event_id=2, name="GW2")
            db.add_all([a, b])
            db.commit()
            assert _resolve_gameweek_id(db, 2) == b.id
    finally:
        engine.dispose()


def test_fixtures_do_not_cross_seasons():
    engine = make_db()
    try:
        with Session(engine) as db:
            old = Season(code="2025-26", display_name="2025/26")
            cur = Season(code="2026-27", display_name="2026/27")
            db.add_all([old, cur])
            db.flush()
            g1 = Gameweek(season_id=old.id, provider_event_id=2, name="GW2")
            g2 = Gameweek(season_id=cur.id, provider_event_id=2, name="GW2")
            db.add_all([g1, g2])
            db.flush()
            teams = [Team(name=f"T{i}", short_name=f"T{i}") for i in range(4)]
            db.add_all(teams)
            db.flush()
            db.add_all(
                [
                    Fixture(
                        season_id=old.id,
                        provider_fixture_id=1,
                        gameweek_id=g1.id,
                        home_team_id=teams[0].id,
                        away_team_id=teams[1].id,
                    ),
                    Fixture(
                        season_id=cur.id,
                        provider_fixture_id=2,
                        gameweek_id=g2.id,
                        home_team_id=teams[2].id,
                        away_team_id=teams[3].id,
                    ),
                ]
            )
            db.commit()
            assert _fixtures_for_gameweek(db, 2) == [
                {"home_team_id": teams[2].id, "away_team_id": teams[3].id}
            ]
    finally:
        engine.dispose()


def test_fixture_count_does_not_raise_multiple_results():
    engine = make_db()
    try:
        with Session(engine) as db:
            old = Season(code="2025-26", display_name="2025/26")
            cur = Season(code="2026-27", display_name="2026/27")
            db.add_all([old, cur])
            db.flush()
            g1 = Gameweek(season_id=old.id, provider_event_id=2, name="GW2")
            g2 = Gameweek(season_id=cur.id, provider_event_id=2, name="GW2")
            db.add_all([g1, g2])
            db.flush()
            t_old = Team(name="OLD", short_name="OLD")
            t_cur = Team(name="CUR", short_name="CUR")
            opp = Team(name="OPP", short_name="OPP")
            db.add_all([t_old, t_cur, opp])
            db.flush()
            player = Player(
                first_name="Test",
                second_name="Player",
                web_name="Test",
                position_code=1,
            )
            db.add(player)
            db.flush()
            db.add_all(
                [
                    PlayerTeamMembership(
                        player_id=player.id,
                        team_id=t_old.id,
                        season_id=old.id,
                    ),
                    PlayerTeamMembership(
                        player_id=player.id,
                        team_id=t_cur.id,
                        season_id=cur.id,
                    ),
                    Fixture(
                        season_id=old.id,
                        provider_fixture_id=10,
                        gameweek_id=g1.id,
                        home_team_id=t_old.id,
                        away_team_id=opp.id,
                    ),
                    Fixture(
                        season_id=cur.id,
                        provider_fixture_id=11,
                        gameweek_id=g2.id,
                        home_team_id=t_cur.id,
                        away_team_id=opp.id,
                    ),
                ]
            )
            db.commit()
            provider = LivePredictionProvider.__new__(LivePredictionProvider)
            provider.session = db
            assert provider.get_fixture_count(player.id, 2) == 1
    finally:
        engine.dispose()


def test_sync_gameweek_creation_is_season_scoped():
    engine = make_db()
    try:
        with Session(engine) as db:
            old = Season(code="2025-26", display_name="2025/26")
            db.add(old)
            db.flush()
            db.add(Gameweek(season_id=old.id, provider_event_id=2, name="GW2"))
            db.commit()
            row = _get_or_create_gameweek(db, 2)
            assert row.season_id != old.id
            assert row.provider_event_id == 2
            assert row.season_id == db.query(Season).filter_by(code="2026-27").one().id
    finally:
        engine.dispose()
