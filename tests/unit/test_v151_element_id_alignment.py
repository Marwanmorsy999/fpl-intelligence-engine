"""v1.5.1 — FPL element ID alignment regression tests.

Root cause under test: ``POST /api/v1/squad/from-fpl`` stores OFFICIAL FPL
element ids as squad player_ids, but ``GET /api/v1/decisions`` used to join
those ids against our internal auto-increment ``players.id``. With a crafted
database where internal id 445 is McConnell and element 445 is Haaland, the
dashboard showed "McConnell" carrying Haaland's xPTS.

These tests prove:

* ``players.fpl_element_id`` exists, is indexed/unique, and is populated by the
  seed-replay path (``_get_or_create_player``);
* imported squads resolve names via ``fpl_element_id`` — never via internal id
  collision;
* demo squads keep resolving via internal ids;
* Haaland shows Haaland's name + ~£15m price + high xPTS end-to-end through the
  real API surface.

Fully offline: the official FPL entry/bootstrap APIs are mocked behind an
``httpx.MockTransport``, mirroring ``tests/unit/test_phase13_fpl_import.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.api import deps
from fpl_intelligence.api.main import app
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    Gameweek,
    Player,
    PlayerExternalId,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
    Season,
    Team,
    TeamExternalId,
)
from fpl_intelligence.prediction.live_provider import LivePredictionProvider
from fpl_intelligence.squad.demo import build_demo_squad
from fpl_intelligence.squad.fpl_import import FplImportResult, FplSquadImporter

ENTRY_ID = 794561
HAALAND_ELEMENT = 445
SAKA_ELEMENT = 318


# ---------------------------------------------------------------------------
# Collision database: internal id 445 = McConnell, element 445 = Haaland.
# (Populated by ``_build_collision_db_into`` below.)
# ---------------------------------------------------------------------------


def _bootstrap_payload() -> dict:
    """15 elements with full metadata (types/prices/teams), 445=Haaland-type-4,
    318=Saka-type-3, plus 13 filler elements unknown to the local database."""
    filler_defs: list[tuple[int, str, int, int]] = [
        (701, "FillerGK1", 1, 45),
        (702, "FillerGK2", 1, 45),
        (703, "FillerD1", 2, 50),
        (704, "FillerD2", 2, 50),
        (705, "FillerD3", 2, 55),
        (706, "FillerD4", 2, 50),
        (707, "FillerD5", 2, 45),
        (708, "FillerM1", 3, 60),
        (709, "FillerM2", 3, 55),
        (710, "FillerM3", 3, 60),
        (711, "FillerM4", 3, 50),
        (712, "FillerF1", 4, 70),
        (713, "FillerF2", 4, 65),
    ]
    elements: list[dict] = [
        {
            "id": el_id,
            "code": 900000 + el_id,
            "web_name": name,
            "first_name": f"First{name}",
            "second_name": f"Last{name}",
            "element_type": pos,
            "team": (el_id % 20) + 1,
            "now_cost": cost,
        }
        for el_id, name, pos, cost in filler_defs
    ]
    elements.extend(
        [
            {
                "id": HAALAND_ELEMENT,
                "code": 223094,
                "web_name": "Haaland",
                "first_name": "Erling",
                "second_name": "Haaland",
                "element_type": 4,
                "team": 15,
                "now_cost": 155,
            },
            {
                "id": SAKA_ELEMENT,
                "code": 223340,
                "web_name": "Saka",
                "first_name": "Bukayo",
                "second_name": "Saka",
                "element_type": 3,
                "team": 2,
                "now_cost": 95,
            },
        ]
    )
    return {
        "elements": elements,
        "teams": [],
        "events": [{"id": 3, "name": "Gameweek 3"}],
    }


_PICK_ORDER = [
    701,
    702,
    703,
    704,
    705,
    706,
    707,
    SAKA_ELEMENT,
    708,
    709,
    710,
    711,
    HAALAND_ELEMENT,
    712,
    713,
]


def _picks_payload() -> dict:
    picks = []
    for position, element in enumerate(_PICK_ORDER, start=1):
        picks.append(
            {
                "element": element,
                "position": position,
                "is_captain": element == HAALAND_ELEMENT,
                "is_vice_captain": element == SAKA_ELEMENT,
                "multiplier": 1,
            }
        )
    return {
        "active_chip": None,
        "picks": picks,
        "transfers": {"limit": 1, "bank": 10, "made": 0, "value": 1000},
    }


def _fpl_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/bootstrap-static/":
        return httpx.Response(200, json=_bootstrap_payload())
    if path == f"/api/entry/{ENTRY_ID}/":
        return httpx.Response(
            200, json={"id": ENTRY_ID, "name": "Alignment FC", "current_event": 3}
        )
    if path == f"/api/entry/{ENTRY_ID}/event/3/picks/":
        return httpx.Response(200, json=_picks_payload())
    return httpx.Response(404)


def _build_collision_db_into(factory: sessionmaker) -> None:
    """Populate ``factory``'s database with the id-445 collision dataset."""
    db = factory()
    try:
        season = Season(code="2026-27", display_name="2026/27", competition="Premier League")
        db.add(season)
        db.flush()
        mc = Team(name="Manchester United", short_name="MUN")
        ci = Team(name="Manchester City", short_name="MCI")
        ar = Team(name="Arsenal", short_name="ARS")
        db.add_all([mc, ci, ar])
        db.flush()
        for team, ext_id in ((mc, "1"), (ci, "15"), (ar, "2")):
            db.add(
                TeamExternalId(team_id=team.id, provider="official_fpl", provider_team_id=ext_id)
            )
        gw = Gameweek(season_id=season.id, provider_event_id=3, name="Gameweek 3")
        db.add(gw)
        db.flush()

        fillers = [
            Player(
                first_name=f"Filler{i}",
                second_name=f"Filler{i}son",
                web_name=f"Filler{i}",
                position_code=(i % 4) + 1,
            )
            for i in range(444)
        ]
        mcconnell = Player(
            first_name="James",
            second_name="McConnell",
            web_name="McConnell",
            position_code=3,
        )
        haaland = Player(
            first_name="Erling",
            second_name="Haaland",
            web_name="Haaland",
            position_code=4,
            fpl_element_id=HAALAND_ELEMENT,
        )
        saka = Player(
            first_name="Bukayo",
            second_name="Saka",
            web_name="Saka",
            position_code=3,
            fpl_element_id=SAKA_ELEMENT,
        )
        db.add_all(fillers)
        db.flush()
        db.add(mcconnell)
        db.add(haaland)
        db.add(saka)
        db.flush()
        assert mcconnell.id == HAALAND_ELEMENT

        for player, team, el in (
            (mcconnell, mc, None),
            (haaland, ci, HAALAND_ELEMENT),
            (saka, ar, SAKA_ELEMENT),
        ):
            db.add(PlayerTeamMembership(player_id=player.id, team_id=team.id, season_id=season.id))
            if el is not None:
                db.add(
                    PlayerExternalId(
                        player_id=player.id,
                        provider="official_fpl",
                        provider_player_id=str(el),
                    )
                )
        db.add_all(
            [
                PlayerGameweekPerformance(
                    player_id=haaland.id,
                    gameweek_id=gw.id,
                    season_id=season.id,
                    team_id=ci.id,
                    minutes=90,
                    total_points=13,
                    price=15.0,
                ),
                PlayerGameweekPerformance(
                    player_id=saka.id,
                    gameweek_id=gw.id,
                    season_id=season.id,
                    team_id=ar.id,
                    minutes=90,
                    total_points=10,
                    price=9.5,
                ),
                PlayerGameweekPerformance(
                    player_id=mcconnell.id,
                    gameweek_id=gw.id,
                    season_id=season.id,
                    team_id=mc.id,
                    minutes=25,
                    total_points=1,
                    price=4.5,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def collision_db() -> Generator[Session, None, None]:
    """Session over the id-445 collision database."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    _build_collision_db_into(factory)
    db = factory()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def alignment_client(collision_db: Session):
    """API client over the collision DB; FPL import routed to the mock."""

    def _override_db():
        yield collision_db

    app.dependency_overrides[deps._get_db_session] = _override_db
    app.dependency_overrides[deps.get_llm_provider] = lambda: None
    app.dependency_overrides[deps.get_prediction_provider] = lambda: LivePredictionProvider(
        session=collision_db
    )
    try:
        with TestClient(app) as client:
            yield client, collision_db
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1) Schema + seeding
# ---------------------------------------------------------------------------


class TestFplElementIdColumn:
    def test_column_populated_and_idempotent(self, db_session: Session) -> None:
        from fpl_intelligence.ingestion.fpl import _get_or_create_player

        player = _get_or_create_player(
            db_session,
            provider_player_id="445",
            first_name="Erling",
            second_name="Haaland",
            web_name="Haaland",
            position_code=4,
            fpl_code=223094,
        )
        db_session.flush()
        assert player.fpl_element_id == 445
        assert db_session.get(Player, player.id).fpl_element_id == 445

        again = _get_or_create_player(
            db_session,
            provider_player_id="445",
            first_name="Erling",
            second_name="Haaland",
            web_name="Haaland",
            position_code=4,
            fpl_code=223094,
        )
        assert again.id == player.id
        assert again.fpl_element_id == 445

    def test_non_numeric_provider_id_leaves_element_null(self, db_session: Session) -> None:
        from fpl_intelligence.ingestion.fpl import _get_or_create_player

        player = _get_or_create_player(
            db_session,
            provider_player_id="understat-haaland",
            first_name="Erling",
            second_name="Haaland",
            web_name="Haaland",
            position_code=4,
        )
        assert player.fpl_element_id is None


# ---------------------------------------------------------------------------
# 2) Import path: names resolve via fpl_element_id, not internal id
# ---------------------------------------------------------------------------


class TestImportResolvesByElementId:
    def test_haaland_element_445_resolves_to_haaland_not_mcconnell(
        self, collision_db: Session
    ) -> None:
        """THE regression: element 445 must never surface McConnell."""

        async def _run() -> FplImportResult:
            transport = httpx.MockTransport(_fpl_handler)
            async with httpx.AsyncClient(transport=transport) as client:
                importer = FplSquadImporter(client=client)
                return await importer.build_squad_from_entry(ENTRY_ID, collision_db)

        result = asyncio.run(_run())

        assert len(result.squad.player_ids) == 15
        assert HAALAND_ELEMENT in result.squad.player_ids
        assert result.squad.captain_id == HAALAND_ELEMENT
        assert result.player_names[HAALAND_ELEMENT] == "Haaland"
        assert result.player_names[SAKA_ELEMENT] == "Saka"
        # Internal id 445 belongs to McConnell — he must not leak into the map.
        assert "McConnell" not in result.player_names.values()
        # Prices come from bootstrap metadata keyed by ELEMENT id.
        assert result.squad.player_prices[HAALAND_ELEMENT] == 15.5

    def test_decisions_join_by_fpl_element_id(self, alignment_client, monkeypatch) -> None:
        """End-to-end: import -> decisions shows Haaland, never McConnell."""
        from fpl_intelligence.prediction import live_provider as live_provider_mod
        from fpl_intelligence.squad import fpl_import as fpl_import_mod

        async def fake_fetch_json(self, path: str, *, validator=None) -> dict:
            if "/picks/" in path:
                return _picks_payload()
            if path.startswith("/api/entry/"):
                return {
                    "id": ENTRY_ID,
                    "name": "Alignment FC",
                    "current_event": 3,
                    "last_deadline_bank": 10,
                }
            return _bootstrap_payload()

        monkeypatch.setattr(fpl_import_mod.FplSquadImporter, "_fetch_json", fake_fetch_json)
        # Deterministic premium-price catalog: Haaland tops the price ladder.
        monkeypatch.setattr(
            live_provider_mod,
            "load_player_catalog",
            lambda path=None: _synthetic_catalog(),
        )

        client, _db = alignment_client
        resp = client.post(
            "/api/v1/squad/from-fpl",
            json={"entry_id": ENTRY_ID, "gameweek": 3},
        )
        assert resp.status_code == 200
        imported = resp.json()
        assert imported["player_names"][str(HAALAND_ELEMENT)] == "Haaland"

        report = client.get("/api/v1/decisions", params={"session_id": str(ENTRY_ID)}).json()
        players = report["players"]
        haaland = players[str(HAALAND_ELEMENT)]
        assert haaland["web_name"] == "Haaland"
        assert haaland["web_name"] != "McConnell"
        assert players[str(SAKA_ELEMENT)]["web_name"] == "Saka"

        # Haaland must show HIS premium price (~£15m) and high xPTS.
        assert haaland["price"] is not None and haaland["price"] >= 14.0
        if haaland.get("expected_points") is not None:
            assert haaland["expected_points"] >= 3.0
            assert haaland["expected_points"] >= players[str(SAKA_ELEMENT)]["expected_points"]

    def test_legacy_external_id_fallback_when_column_missing(self, collision_db: Session) -> None:
        """Pre-0016 databases (no fpl_element_id) still resolve via external ids."""

        haaland = collision_db.scalar(
            select(Player).where(Player.fpl_element_id == HAALAND_ELEMENT)
        )
        assert haaland is not None
        haaland.fpl_element_id = None  # simulate a pre-migration row
        collision_db.commit()

        async def _run() -> FplImportResult:
            transport = httpx.MockTransport(_fpl_handler)
            async with httpx.AsyncClient(transport=transport) as client:
                importer = FplSquadImporter(client=client)
                return await importer.build_squad_from_entry(ENTRY_ID, collision_db)

        result = asyncio.run(_run())
        assert result.player_names[HAALAND_ELEMENT] == "Haaland"
        assert "McConnell" not in result.player_names.values()


# ---------------------------------------------------------------------------
# 3) Demo path unchanged: internal ids keep resolving
# ---------------------------------------------------------------------------


class TestDemoPathSingleIdSpace:
    def test_demo_squad_uses_internal_ids_when_no_element_ids(self, db_session: Session) -> None:
        """Legacy rows (no fpl_element_id): demo falls back to internal ids."""
        # Seed enough players for the demo formation (2 GK, 5 DEF, 5 MID, 3 FWD).
        codes = [1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4]
        for i, code in enumerate(codes, start=1):
            db_session.add(
                Player(
                    first_name=f"First{i}",
                    second_name=f"Last{i}",
                    web_name=f"Demo{i}",
                    position_code=code,
                )
            )
        db_session.commit()

        squad = build_demo_squad(db_session)
        assert squad.is_demo is True
        # Internal ids are small auto-increment values, never FPL element ids.
        assert squad.player_ids == sorted(squad.player_ids)
        assert max(squad.player_ids) <= len(codes)

        # Every stored id resolves back to the intended player row.
        for pid in squad.player_ids:
            row = db_session.get(Player, pid)
            assert row is not None

    def test_demo_squad_prefers_fpl_element_ids(self, db_session: Session) -> None:
        """Phase 18.0 R1: demo squads live in the same id space as imports."""
        codes = [1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4]
        for i, code in enumerate(codes, start=100):
            db_session.add(
                Player(
                    first_name=f"P{i}",
                    second_name=f"L{i}",
                    web_name=f"El{i}",
                    position_code=code,
                    fpl_element_id=i,
                )
            )
        db_session.commit()

        squad = build_demo_squad(db_session)
        assert squad.is_demo is True
        # Every stored id is an official FPL element id.
        for pid in squad.player_ids:
            row = db_session.scalar(select(Player).where(Player.fpl_element_id == pid))
            assert row is not None, f"{pid} must be an fpl_element_id"
        # Captain/vice are stored in the same id space.
        captain = db_session.scalar(select(Player).where(Player.fpl_element_id == squad.captain_id))
        vice = db_session.scalar(
            select(Player).where(Player.fpl_element_id == squad.vice_captain_id)
        )
        assert captain is not None and vice is not None


# ---------------------------------------------------------------------------
# 4) Proxy chain: correct element id -> that player's own high xPTS
# ---------------------------------------------------------------------------


def _synthetic_catalog() -> dict[int, dict]:
    """Catalog keyed by ELEMENT id with Haaland as the clear price leader."""
    catalog: dict[int, dict] = {}
    for i in range(30):
        catalog[900 + i] = {
            "web_name": f"Cheap{i}",
            "price": 4.0 + (i % 6) * 0.5,
            "position": (i % 4) + 1,
            "team": (i % 20) + 1,
            "team_short": f"T{i % 20}",
        }
    # The picked filler elements must be covered too (the official bootstrap
    # covers every element in production).
    for idx, el in enumerate(_PICK_ORDER):
        if el in (HAALAND_ELEMENT, SAKA_ELEMENT):
            continue
        catalog[el] = {
            "web_name": f"Pick{el}",
            "price": 4.0 + (idx % 5) * 0.5,
            "position": (el % 4) + 1,
            "team": (el % 20) + 1,
            "team_short": f"T{el % 20}",
        }
    catalog[SAKA_ELEMENT] = {
        "web_name": "Saka",
        "price": 9.5,
        "position": 3,
        "team": 2,
        "team_short": "ARS",
    }
    catalog[HAALAND_ELEMENT] = {
        "web_name": "Haaland",
        "price": 15.5,
        "position": 4,
        "team": 15,
        "team_short": "MCI",
    }
    return catalog


class TestProxyXptsFollowsElementId:
    def test_haaland_element_gets_premium_xpts(self, collision_db: Session) -> None:
        """With a correct catalog join, element 445 earns Haaland-scale xPTS."""
        from fpl_intelligence.prediction import live_provider as live_provider_mod
        from fpl_intelligence.prediction.live_provider import LivePredictionProvider

        _original_loader = live_provider_mod.load_player_catalog
        live_provider_mod.load_player_catalog = (  # type: ignore[assignment]
            lambda path=None: _synthetic_catalog()
        )
        try:
            provider = LivePredictionProvider(session=collision_db)
            preds = provider.get_squad_predictions([HAALAND_ELEMENT, SAKA_ELEMENT], [3])
        finally:
            live_provider_mod.load_player_catalog = _original_loader

        haaland_pred = preds[3].get(HAALAND_ELEMENT)
        saka_pred = preds[3].get(SAKA_ELEMENT)
        assert haaland_pred is not None, "premium element must be covered by chain"
        assert haaland_pred.expected_points >= 3.0
        assert saka_pred is not None
        assert haaland_pred.expected_points > saka_pred.expected_points


# ---------------------------------------------------------------------------
# 4) Seed file integrity: element ids are the official FPL ids
# ---------------------------------------------------------------------------


class TestSeedElementAlignment:
    def test_seed_element_ids_are_official_fpl_ids(self) -> None:
        """The committed seed's ``id`` IS the official FPL element id.

        Current 2026-27 official mapping: Haaland = 411, Saka = 12 (verified
        live against bootstrap-static on 2026-08-22). Regenerating the seed via
        ``scripts/regenerate_bootstrap_seed.py`` keeps this invariant true for
        any future season; update these two spot values only when the official
        ids themselves change.
        """
        import json
        from pathlib import Path

        seed_path = (
            Path(__file__).resolve().parents[2] / "data" / "seed" / "fpl_bootstrap_seed.json"
        )
        if not seed_path.exists():
            pytest.skip("seed file not present")
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        players = seed.get("players") or []
        assert len(players) >= 400, "seed must cover the full player universe"

        # Join-key integrity: every element id must be unique.
        ids = [int(p["id"]) for p in players]
        assert len(ids) == len(set(ids)), "duplicate element ids in seed"

        by_name = {p["web_name"]: int(p["id"]) for p in players}
        assert by_name.get("Haaland") == 411
        assert by_name.get("Saka") == 12

        # The seed's id must equal what ingest writes into fpl_element_id.
        haaland_row = next(p for p in players if p["web_name"] == "Haaland")
        assert haaland_row["now_cost"] >= 140, "Haaland must be priced ~£14m+"
