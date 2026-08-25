"""v2.7.4-prod-heal — regression tests for the prod 500s + season guard.

Reproduces the prod failure mode deterministically: a database where
migration 0021 was never applied (``local_squad_state`` absent) while cached
league rows exist, exactly like the live deployment.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Register every table on the shared metadata (same as tests/conftest.py).
import fpl_intelligence.leagues.models as _league_models  # noqa: F401,E402
import fpl_intelligence.squad.models_db as _squad_models  # noqa: E402
import fpl_intelligence.sync.materialized_models as _mm  # noqa: F401,E402
import fpl_intelligence.sync.models as _sm  # noqa: F401,E402
import fpl_intelligence.transfers.models as _tm  # noqa: E402,F401
from fpl_intelligence import __version__
from fpl_intelligence.api import deps
from fpl_intelligence.db.base import Base


@pytest.fixture()
def pre_0021_db() -> Generator[Session, None, None]:
    """SQLite with ALL tables except ``local_squad_state`` — prod's state."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record):  # pragma: no cover - parity with conftest
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    for t in Base.metadata.tables.values():
        t.create(engine)
    # Simulate the un-migrated prod DB.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE local_squad_state"))

    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = maker()
    try:
        yield session
    finally:
        session.close()


def _seed_league_cache(db: Session, sid: str = "2295006") -> None:
    now = datetime.now(UTC)
    db.execute(
        _squad_models.SquadStateDB.__table__.insert().values(
            session_id=sid,
            squad_json={
                "gameweek": 1,
                "player_ids": list(range(1, 16)),
                "captain_id": 1,
                "vice_captain_id": 2,
                "bank": 1.0,
                "free_transfers": 2,
                "chips_available": ["wildcard", "free_hit"],
                "player_prices": {},
            },
            updated_at=now,
        )
    )
    db.execute(
        _league_models.LeagueCacheDB.__table__.insert().values(
            league_id=12345,
            name="Heal League",
            member_count=4,
            standings=[
                {"entry_id": int(sid), "entry_name": "Me", "rank": 1, "total": 100},
                {"entry_id": 999, "entry_name": "Rival A", "rank": 2, "total": 95},
                {"entry_id": 998, "entry_name": "Rival B", "rank": 3, "total": 90},
                {"entry_id": 997, "entry_name": "Rival C", "rank": 4, "total": 85},
            ],
            rivals_picks={"picks": {}, "captains": {}, "gameweek": 1},
            refreshed_at=now,
        )
    )
    # Discovered leagues pre-seeded so the route never attempts live FPL
    # discovery during tests.
    db.execute(
        _league_models.EntryLeagueDB.__table__.insert().values(
            entry_id=int(sid),
            league_id=12345,
            league_name="Heal League",
            member_count=4,
            private=False,
            discovered_at=now,
        )
    )
    db.commit()


class TestLeagueNever500:
    """GET /league must never surface a 500 when 0021 is absent."""

    def test_league_ok_and_self_seals_table(self, pre_0021_db: Session) -> None:
        from fpl_intelligence.api.main import app

        _seed_league_cache(pre_0021_db)

        def _override() -> Generator[Session, None, None]:
            yield pre_0021_db

        app.dependency_overrides[deps._get_db_session] = _override
        try:
            client = TestClient(app)
            r = client.get("/api/v1/league", params={"session_id": "2295006"})
            assert r.status_code == 200, r.text[:400]
            body = r.json()
            assert body["status"] in {"ok", "stale", "refreshing", "degraded"}
            # Self-sealing: the read created the missing table.
            insp = inspect(pre_0021_db.get_bind())
            assert insp.has_table("local_squad_state")
        finally:
            app.dependency_overrides.pop(deps._get_db_session, None)

    def test_trajectory_never_500(self, pre_0021_db: Session) -> None:
        from fpl_intelligence.api.main import app

        _seed_league_cache(pre_0021_db)

        def _override() -> Generator[Session, None, None]:
            yield pre_0021_db

        app.dependency_overrides[deps._get_db_session] = _override
        try:
            client = TestClient(app)
            r = client.get(
                "/api/v1/league/trajectory", params={"session_id": "2295006"}
            )
            assert r.status_code == 200, r.text[:400]
            body = r.json()
            assert isinstance(body.get("series"), list)
            if body.get("status") != "ok":
                assert "trajectory unavailable" in str(body.get("note", "")) or body[
                    "status"
                ] in {"no-predictions", "no-cache", "no-league", "unavailable"}
        finally:
            app.dependency_overrides.pop(deps._get_db_session, None)

    def test_effective_squad_falls_back_to_base(self, pre_0021_db: Session) -> None:
        from fpl_intelligence.squad.service import SquadService

        _seed_league_cache(pre_0021_db)
        svc = SquadService(session=pre_0021_db)
        squad = svc.get_effective_squad("2295006")
        assert squad is not None, "base squad fallback broken"
        assert list(squad.player_ids) == list(range(1, 16))

    def test_local_squad_read_degrades_to_none(self, pre_0021_db: Session) -> None:
        from fpl_intelligence.squad.service import SquadService

        _seed_league_cache(pre_0021_db)
        svc = SquadService(session=pre_0021_db)
        assert svc.get_local_squad("2295006") is None


class TestFomoSeasonGuard:
    """FOMO must grade within the CURRENT season's GW range, never MAX(gw)."""

    def test_fomo_ignores_last_season_gw38(self, db_session: Session) -> None:
        from fpl_intelligence.sync.gameweek_clock import (
            resolve_season_gw_ceiling_sync,
        )
        from fpl_intelligence.sync.materialized_models import FixturesCacheDB
        from fpl_intelligence.sync.models import IngestedGameweekDB
        from fpl_intelligence.track_record.fomo import compute_regret

        # Last season's rows: GW38 of 2025/26 sits in ingested_history.
        db_session.add(
            IngestedGameweekDB(
                gameweek=38,
                element_id=1,
                total_points=10,
                ingested_at=datetime.now(UTC),
            )
        )
        # Fixtures cache implying we are early in the CURRENT season.
        db_session.add(
            FixturesCacheDB(
                payload=[
                    {"event": 1, "team_h": 1, "team_a": 2, "finished": True},
                    {"event": 2, "team_h": 1, "team_a": 2, "finished": False},
                ],
                fetched_at=datetime.now(UTC),
            )
        )
        db_session.commit()

        ceiling = resolve_season_gw_ceiling_sync(db_session)
        assert ceiling == 2, f"clock must bound to current season, got {ceiling}"

        report = compute_regret(db_session, "2295006", None)
        assert report["gameweek"] != 38
        assert report["gameweek"] in (None, 2)  # within-range only
        assert report["status"] in {
            "unavailable",
            "no-actuals",
            "no-recommendations",
            "ok",
        }

    def test_fomo_explicit_future_gw_clamped(self, db_session: Session) -> None:
        from fpl_intelligence.sync.gameweek_clock import (
            resolve_season_gw_ceiling_sync,
        )
        from fpl_intelligence.sync.materialized_models import FixturesCacheDB
        from fpl_intelligence.track_record.fomo import compute_regret

        db_session.add(
            FixturesCacheDB(
                payload=[
                    {"event": 1, "team_h": 1, "team_a": 2, "finished": True},
                    {"event": 2, "team_h": 3, "team_a": 4, "finished": False},
                ],
                fetched_at=datetime.now(UTC),
            )
        )
        db_session.commit()
        assert resolve_season_gw_ceiling_sync(db_session) == 2

        report = compute_regret(db_session, "2295006", 38)
        assert report["gameweek"] <= 2
        assert report.get("season_note"), "clamping must be disclosed"


def test_version_is_semver_prefixed() -> None:
    """v2.7.4: /health must serve a plain MAJOR.MINOR.PATCH truth string."""
    assert __version__.startswith("2.7.")
