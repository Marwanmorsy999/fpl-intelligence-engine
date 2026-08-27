"""v2.7.5-decisions-heal — regression tests for the decisions skeleton and
the /league 500 observed in production at 2026-08-26T03:20Z.

Reproduced deterministically:

* an ``local_squad_state`` row whose ``squad_json`` carries no players won the
  dual-state read and ``GET /decisions`` silently served
  ``{"generated_at": ...}`` — the builder now falls back to the base squad and
  refuses to serve a hollow report;
* a failure raised BEFORE the route handler (e.g. the ``get_db`` dependency)
  bypassed the v2.7.4 in-handler try/except and surfaced as a raw 500 — a
  ServerErrorMiddleware-level safety net now converts it into an honest
  degraded payload.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import fpl_intelligence.leagues.models as _league_models  # noqa: F401,E402
import fpl_intelligence.squad.models_db as _squad_models  # noqa: F401,E402
import fpl_intelligence.sync.materialized_models as _mm  # noqa: F401,E402
import fpl_intelligence.sync.models as _sm  # noqa: F401,E402
import fpl_intelligence.transfers.models as _tm  # noqa: F401,E402
from fpl_intelligence.api import deps
from fpl_intelligence.db.base import Base
from fpl_intelligence.live_intelligence.bridge import StaticPredictionProvider


@pytest.fixture()
def heal_db() -> Generator[Session, None, None]:
    from sqlalchemy import create_engine, event

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

    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(heal_db: Session) -> Generator[TestClient, None, None]:
    """TestClient with DB + provider overrides and a clean decisions cache."""
    from fpl_intelligence.api.main import app
    from fpl_intelligence.api.routes.squad import _decisions_cache, _decisions_cache_lock

    def _override_db() -> Generator[Session, None, None]:
        yield heal_db

    app.dependency_overrides[deps._get_db_session] = _override_db
    app.dependency_overrides[deps.get_prediction_provider] = (
        lambda: StaticPredictionProvider()
    )
    with _decisions_cache_lock:
        _decisions_cache.clear()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(deps._get_db_session, None)
        app.dependency_overrides.pop(deps.get_prediction_provider, None)


def _seed_base_squad(db: Session, sid: str = "2295006") -> None:
    db.execute(
        _squad_models.SquadStateDB.__table__.insert().values(
            session_id=sid,
            squad_json={
                "gameweek": 2,
                "player_ids": list(range(1, 16)),
                "captain_id": 3,
                "vice_captain_id": 13,
                "bank": 1.0,
                "free_transfers": 1,
                "chips_available": ["wildcard", "free_hit"],
                "player_positions": {
                    i: (1 if i <= 2 else (2 if i <= 7 else (3 if i <= 12 else 4)))
                    for i in range(1, 16)
                },
                "player_prices": {i: 7.5 for i in range(1, 16)},
                "player_teams": {i: 1 for i in range(1, 16)},
            },
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()


class TestDecisionsFullChain:
    """GET /decisions must serve the FULL chain, never a bare generated_at."""

    REQUIRED_KEYS = (
        "generated_at",
        "gameweek",
        "starting_xi",
        "bench_order",
        "captain",
        "vice_captain",
        "transfer_plan",
        "chip_recommendation",
        "players",
    )

    def test_full_payload_from_base_squad(
        self, client: TestClient, heal_db: Session
    ) -> None:
        _seed_base_squad(heal_db)
        r = client.get("/api/v1/decisions", params={"session_id": "2295006"})
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        for key in self.REQUIRED_KEYS:
            assert key in body, f"missing key {key}"
        assert len(body["starting_xi"]) == 11
        assert body["captain"] is not None and body["captain"]["player_id"] > 0
        assert isinstance(body["transfer_plan"], dict)
        assert len(body["players"]) == 15

    def test_empty_local_squad_falls_back_to_base(
        self, client: TestClient, heal_db: Session
    ) -> None:
        """The prod incident: local_squad_state wins the read but is empty.

        The corrupt row is written directly (bypassing pydantic's 15-player
        validation) exactly like the un-migrated prod table.
        """
        _seed_base_squad(heal_db)
        heal_db.execute(
            _squad_models.LocalSquadStateDB.__table__.insert().values(
                session_id="2295006",
                squad_json={
                    "gameweek": 2,
                    "player_ids": [],
                    "captain_id": 0,
                    "vice_captain_id": 0,
                    "bank": 0.0,
                    "free_transfers": 1,
                    "chips_available": [],
                },
                updated_at=datetime.now(UTC),
            )
        )
        heal_db.commit()

        r = client.get("/api/v1/decisions", params={"session_id": "2295006"})
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        assert len(body["starting_xi"]) > 0, "must fall back to base squad"
        assert body["captain"] is not None
        assert len(body["players"]) == 15

    def test_no_squad_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/decisions", params={"session_id": "ghost"})
        assert r.status_code == 404

    def test_internal_failure_is_honest_503_never_bare_500(
        self, client: TestClient, heal_db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_base_squad(heal_db)
        from fpl_intelligence.api.routes import squad as squad_route

        def _boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("optimizer exploded")

        monkeypatch.setattr(squad_route.SquadService, "_read_local_row", _boom)
        # get_effective_squad swallows internally → returns None; get_squad too?
        # get_squad does NOT swallow: it would raise inside the service... it
        # uses scalar_one_or_none via execute which now explodes → caught by
        # the route's catch-all → honest 503.
        monkeypatch.setattr(squad_route.SquadService, "get_squad", _boom)

        r = client.get(
            "/api/v1/decisions",
            params={"session_id": "2295006"},
        )
        assert r.status_code == 503, r.text[:400]
        assert "Decisions engine" in r.json()["detail"]

    def test_build_decisions_payload_guard_rejects_hollow_report(
        self, heal_db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A populated squad + empty optimizer output must raise loudly."""
        import asyncio

        from fpl_intelligence.api.routes.squad import build_decisions_payload
        from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
        from fpl_intelligence.squad.models import DecisionReport

        _seed_base_squad(heal_db)

        def _hollow(self: object, _squad: object) -> DecisionReport:
            return DecisionReport(gameweek=2)

        monkeypatch.setattr(DecisionOptimizerBridge, "generate_decisions", _hollow)

        async def _run() -> None:
            with pytest.raises(RuntimeError, match="empty starting XI"):
                await build_decisions_payload(
                    heal_db, StaticPredictionProvider(), "2295006"
                )

        asyncio.run(_run())


class TestLeagueNever500Net:
    """The NEVER-500 contract must hold even for pre-handler explosions."""

    def test_handler_body_failure_returns_200_degraded(
        self, client: TestClient, heal_db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fpl_intelligence.api.routes import league as league_route

        async def _boom(*_a: object, **_k: object) -> dict[str, object]:
            raise RuntimeError("impl exploded")

        monkeypatch.setattr(league_route, "_league_overview_impl", _boom)
        r = client.get("/api/v1/league", params={"session_id": "2295006"})
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        assert body["status"] == "degraded"
        assert "RuntimeError" in body.get("diag", "")

    def test_pre_handler_db_failure_returns_200_degraded(
        self, heal_db: Session
    ) -> None:
        """Dependency-stage DB explosion must NOT surface as a raw 500."""
        from fpl_intelligence.api.main import app

        def _broken_db() -> Generator[Session, None, None]:
            raise RuntimeError("connection refused (pool exhausted)")
            yield  # pragma: no cover - unreachable, keeps generator semantics

        app.dependency_overrides[deps._get_db_session] = _broken_db
        try:
            # raise_server_exceptions=False mirrors real server behavior:
            # ServerErrorMiddleware re-raises after sending OUR response.
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/v1/league", params={"session_id": "2295006"})
            assert r.status_code == 200, r.text[:400]
            body = r.json()
            assert body["status"] == "degraded"
            assert "diag" not in body
            assert "RuntimeError" not in r.text
        finally:
            app.dependency_overrides.pop(deps._get_db_session, None)

    def test_league_success_still_carries_rank(
        self, client: TestClient, heal_db: Session
    ) -> None:
        """Happy path intact: seeded cache yields rank + standings_top."""
        now = datetime.now(UTC)
        heal_db.execute(
            _league_models.LeagueCacheDB.__table__.insert().values(
                league_id=12345,
                name="Heal League",
                member_count=4,
                standings=[
                    {"entry_id": 2295006, "entry_name": "Me", "rank": 1, "total": 100},
                    {"entry_id": 999, "entry_name": "Rival A", "rank": 2, "total": 95},
                    {"entry_id": 998, "entry_name": "Rival B", "rank": 3, "total": 90},
                    {"entry_id": 997, "entry_name": "Rival C", "rank": 4, "total": 85},
                ],
                rivals_picks={"picks": {}, "captains": {}, "gameweek": 1},
                refreshed_at=now,
            )
        )
        heal_db.execute(
            _league_models.EntryLeagueDB.__table__.insert().values(
                entry_id=2295006,
                league_id=12345,
                league_name="Heal League",
                member_count=4,
                private=False,
                discovered_at=now,
            )
        )
        heal_db.commit()
        r = client.get("/api/v1/league", params={"session_id": "2295006"})
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        assert body.get("your_rank") == 1 or body.get("standings_top")
