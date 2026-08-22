"""Phase 13.0 - One-click FPL squad import tests.

Mocks the official FPL Entry API (entry summary, per-gameweek picks, and
bootstrap-static) behind an ``httpx.MockTransport`` with zero network access, and
verifies the response is correctly mapped into the internal
:class:`~fpl_intelligence.squad.models.SquadStateCreate` (and the players table
when available). A small FastAPI ``TestClient`` suite exercises the
``POST /api/v1/squad/from-fpl`` endpoint end-to-end: persistence, the friendly
error messages, and the follow-up ``GET /api/v1/decisions`` call.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.api import deps
from fpl_intelligence.api.main import app
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import Player, PlayerExternalId
from fpl_intelligence.squad import fpl_import as fpl_import_mod
from fpl_intelligence.squad.fpl_import import (
    FplApiUnavailable,
    FplEntryNotFound,
    FplImportResult,
    FplPicksNotSaved,
    FplSquadImporter,
)
from fpl_intelligence.squad.models import SquadStateCreate

ENTRY_ID = 1234567


def _bootstrap() -> dict:
    elements = []
    for i in range(1, 16):
        elements.append(
            {
                "id": i,
                "web_name": f"Player{i}",
                "first_name": f"First{i}",
                "second_name": f"Last{i}",
                "element_type": (i % 4) + 1,
                "team": (i % 20) + 1,
                "now_cost": 50 + i,
            }
        )
    return {"elements": elements}


def _picks() -> dict:
    picks = []
    for i in range(1, 16):
        picks.append(
            {
                "element": i,
                "position": i,
                "is_captain": i == 3,
                "is_vice_captain": i == 11,
                "multiplier": 1,
            }
        )
    return {
        "active_chip": None,
        "picks": picks,
        "transfers": {"limit": 1, "bank": 25, "made": 0, "value": 1000, "status": "{}"},
    }


def _entry() -> dict:
    return {
        "id": ENTRY_ID,
        "name": "Test FC",
        "current_event": 8,
        "last_deadline_bank": 5,
        "last_deadline_total_transfers": 2,
        "chips": [
            {"chip": "wildcard", "event": 4},
            {"chip": "freehit", "event": None},
            {"chip": "bboost", "event": None},
            {"chip": "3xc", "event": 3},
        ],
    }


def _handler_factory(entry=None, picks=None, bootstrap=None, error=None):
    entry = entry or _entry()
    picks = picks or _picks()
    bootstrap = bootstrap or _bootstrap()

    async def handler(request: httpx.Request) -> httpx.Response:
        if error is not None:
            raise error
        path = str(request.url.path)
        if path == f"/api/entry/{ENTRY_ID}/":
            return httpx.Response(200, json=entry)
        if path == f"/api/entry/{ENTRY_ID}/event/8/picks/":
            return httpx.Response(200, json=picks)
        if path == "/api/bootstrap-static/":
            return httpx.Response(200, json=bootstrap)
        return httpx.Response(404)

    return handler


async def _run(handler, db=None) -> FplImportResult:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        importer = FplSquadImporter(client=client)
        return await importer.build_squad_from_entry(ENTRY_ID, db)


class TestFplSquadImporterMapping:
    """Verify the FPL payload maps into the internal SquadState."""

    def test_maps_picks_to_squad_state(self) -> None:
        result = asyncio.run(_run(_handler_factory()))
        squad = result.squad
        assert isinstance(squad, SquadStateCreate)
        assert squad.player_ids == list(range(1, 16))
        assert squad.captain_id == 3
        assert squad.vice_captain_id == 11
        # FPL stores money in tenths of a million (25 -> 2.5m).
        assert squad.bank == pytest.approx(2.5)
        assert squad.free_transfers == 1
        assert squad.gameweek == 8
        # element 1 -> element_type 2 (DEF), now_cost 51 -> 5.1m, team 2.
        assert squad.player_positions[1] == 2
        assert squad.player_prices[1] == pytest.approx(5.1)
        assert squad.player_teams[1] == 2
        # wildcard & triple_captain already played -> only free_hit & bench_boost left.
        assert set(squad.chips_available) == {"free_hit", "bench_boost"}

    def test_player_names_from_bootstrap(self) -> None:
        result = asyncio.run(_run(_handler_factory()))
        assert result.player_names[1] == "Player1"
        assert result.player_names[15] == "Player15"
        assert result.entry_name == "Test FC"
        assert result.gameweek == 8

    def test_entry_not_found_raises(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        with pytest.raises(FplEntryNotFound):
            asyncio.run(_run(handler))

    def test_api_unavailable_on_connect_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        with pytest.raises(FplApiUnavailable):
            asyncio.run(_run(handler))

    def test_resolves_names_from_players_table(self, db_session) -> None:
        player = Player(
            first_name="Real",
            second_name="Name",
            web_name="R. Name",
            position_code=3,
        )
        db_session.add(player)
        db_session.flush()
        db_session.add(
            PlayerExternalId(player_id=player.id, provider="fpl", provider_player_id="1")
        )
        db_session.commit()

        result = asyncio.run(_run(_handler_factory(), db=db_session))
        # element 1 resolved via the fpl external id -> canonical web_name.
        assert result.player_names[1] == "R. Name"
        # element 2 not ingested locally -> bootstrap-static fallback.
        assert result.player_names[2] == "Player2"


def _canned_result() -> FplImportResult:
    squad = SquadStateCreate(
        player_ids=list(range(1, 16)),
        captain_id=3,
        vice_captain_id=11,
        bank=0.5,
        free_transfers=1,
        chips_available=["wildcard", "free_hit", "bench_boost", "triple_captain"],
        gameweek=8,
        player_positions={i: (i % 4) + 1 for i in range(1, 16)},
        player_prices={i: 5.0 for i in range(1, 16)},
        player_teams={i: 1 for i in range(1, 16)},
    )
    return FplImportResult(
        squad=squad,
        player_names={i: f"Player{i}" for i in range(1, 16)},
        entry_name="Test FC",
        gameweek=8,
    )


@pytest.fixture
def client():
    """TestClient backed by a shared in-memory SQLite database."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        db = SL()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[deps._get_db_session] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)


class TestFromFplEndpoint:
    """End-to-end wiring of POST /api/v1/squad/from-fpl."""

    def test_saves_squad_and_serves_decisions(self, client, monkeypatch) -> None:
        async def fake(entry_id, db=None):
            return _canned_result()

        monkeypatch.setattr(
            fpl_import_mod.FplSquadImporter, "build_squad_from_entry", staticmethod(fake)
        )

        resp = client.post("/api/v1/squad/from-fpl", json={"entry_id": ENTRY_ID})
        assert resp.status_code == 200
        data = resp.json()
        assert data["squad"]["player_ids"] == list(range(1, 16))
        assert data["entry_name"] == "Test FC"
        assert data["player_names"]["1"] == "Player1"

        dec = client.get("/api/v1/decisions", params={"session_id": str(ENTRY_ID)})
        assert dec.status_code == 200
        assert dec.json()["gameweek"] == 8

    def test_unknown_team_id_returns_friendly_404(self, client, monkeypatch) -> None:
        async def fake(entry_id, db=None):
            raise FplEntryNotFound("nope")

        monkeypatch.setattr(
            fpl_import_mod.FplSquadImporter, "build_squad_from_entry", staticmethod(fake)
        )

        resp = client.post("/api/v1/squad/from-fpl", json={"entry_id": 1})
        assert resp.status_code == 404
        assert "Could not find FPL Team ID" in resp.json()["detail"]

    def test_api_down_returns_503(self, client, monkeypatch) -> None:
        async def fake(entry_id, db=None):
            raise FplApiUnavailable("down")

        monkeypatch.setattr(
            fpl_import_mod.FplSquadImporter, "build_squad_from_entry", staticmethod(fake)
        )

        resp = client.post("/api/v1/squad/from-fpl", json={"entry_id": 1})
        assert resp.status_code == 503
        assert "temporarily down" in resp.json()["detail"]


@pytest.fixture
def seeded_client():
    """TestClient backed by an in-memory SQLite DB seeded with 20 players."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with SL() as s:
        # 2 GK, 5 DEF, 5 MID, 5 FWD (>= 2/5/5/3 needed for the demo squad).
        codes = [1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4]
        for i, code in enumerate(codes, start=1):
            s.add(
                Player(
                    first_name=f"First{i}",
                    second_name=f"Last{i}",
                    web_name=f"Demo{i}",
                    position_code=code,
                )
            )
        s.commit()

    def override():
        db = SL()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[deps._get_db_session] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)


class TestDemoSquad:
    """POST /api/v1/squad/demo builds a valid squad from seeded DB players."""

    def test_demo_builds_valid_squad_from_db(self, seeded_client) -> None:
        resp = seeded_client.post("/api/v1/squad/demo", params={"session_id": "demo-e2e"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        squad = data["squad"]

        # Marker flags + 15 players, valid formation.
        assert squad["is_demo"] is True
        assert data["is_demo"] is True
        assert len(squad["player_ids"]) == 15
        counts = Counter(int(v) for v in squad["player_positions"].values())
        assert counts == {1: 2, 2: 5, 3: 5, 4: 3}

        # Sensible captain/vice and bank/transfers.
        assert squad["captain_id"] in squad["player_ids"]
        assert squad["vice_captain_id"] in squad["player_ids"]
        assert squad["captain_id"] != squad["vice_captain_id"]
        assert squad["bank"] == pytest.approx(2.0)
        assert squad["free_transfers"] == 1

        # Names + prices always render (real DB players).
        assert len(data["player_names"]) == 15
        assert all(data["player_names"].values())
        assert all(squad["player_prices"].values())

        # Renders exactly like a real squad via GET /api/v1/decisions.
        dec = seeded_client.get("/api/v1/decisions", params={"session_id": "demo-e2e"})
        assert dec.status_code == 200
        assert dec.json()["gameweek"] == 1


class TestPicksNotSavedMessage:
    """The picks-404 path returns a 409."""

    def test_preseason_picks_404_returns_friendly_message(self, client, monkeypatch) -> None:
        async def fake(entry_id, db=None):
            raise FplPicksNotSaved("Picks not saved yet")

        monkeypatch.setattr(
            fpl_import_mod.FplSquadImporter, "build_squad_from_entry", staticmethod(fake)
        )

        resp = client.post("/api/v1/squad/from-fpl", json={"entry_id": 794561})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Picks not saved yet"
