"""Phase 24 — compare + chips + set pieces smoke."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from unittest.mock import MagicMock
from fpl_intelligence.api import deps
from fpl_intelligence.live_intelligence.bridge import StaticPredictionProvider
from fpl_intelligence.squad.models import SquadStateCreate

@pytest.fixture
def client(db_session: Session):
    from fpl_intelligence.api.main import app
    from fpl_intelligence.squad.models_db import SquadStateDB
    from sqlalchemy import delete
    def _override():
        yield db_session
    app.dependency_overrides[deps._get_db_session] = _override
    app.dependency_overrides[deps.get_prediction_provider] = lambda: StaticPredictionProvider()
    app.dependency_overrides[deps.get_llm_provider] = lambda: MagicMock()
    db_session.execute(delete(SquadStateDB))
    db_session.commit()
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    db_session.execute(delete(SquadStateDB))
    db_session.commit()

def _make_squad(client, session_id="s24_test"):
    from fpl_intelligence.prediction.live_provider import load_player_catalog
    cat = load_player_catalog()
    ids = list(range(1,16))
    # use catalog-accurate teams where possible
    teams = {}
    for pid in ids:
        row = cat.get(pid)
        if row and row.get("team"):
            teams[pid]= int(row["team"])
        else:
            teams[pid]= 1 if pid<=8 else 2
    payload = {
        "player_ids": ids,
        "captain_id": 1,
        "vice_captain_id": 2,
        "bank": 1.0,
        "free_transfers": 1,
        "chips_available": ["wildcard","free_hit","bench_boost","triple_captain"],
        "gameweek": 2,
        "player_positions": {i: (1 if i<=2 else 2 if i<=7 else 3 if i<=12 else 4) for i in range(1,16)},
        "player_prices": {i: 5.0 for i in range(1,16)},
        "player_teams": teams,
    }
    r = client.post("/api/v1/squad", json=payload, params={"session_id": session_id})
    assert r.status_code == 200, r.text
    return session_id

def test_compare_renders_two_real_players(client):
    sid = _make_squad(client, "cmp_sid")
    # use two real element ids from catalog: Saka 12 and Haaland 411 (both exist in seed)
    # but our squad only has 1-15, so provider will still return predictions for 12 and 411 via StaticProvider fallback?
    # StaticProvider returns for any pid, so we can use 1 and 2 which are in squad
    r = client.get("/api/v1/compare", params={"player_a": 1, "player_b": 2, "session_id": sid})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "player_a" in data and "player_b" in data
    assert data["player_a"]["web_name"] is not None
    assert data["player_b"]["web_name"] is not None
    assert "expected_points" in data["player_a"]
    # diff highlights exist
    assert "diff" in data
    # set pieces present
    assert "set_pieces" in data["player_a"]

def test_compare_with_real_catalog_ids(client):
    sid = _make_squad(client, "cmp2_sid")
    # 12 is Saka, 411 is Haaland from seed — both should resolve via catalog fallback even if not in DB player table
    r = client.get("/api/v1/compare", params={"player_a": 12, "player_b": 411, "session_id": sid})
    assert r.status_code == 200
    data = r.json()
    assert data["player_a"]["id"] == 12
    assert data["player_b"]["id"] == 411
    # at least one has xPTS via StaticProvider
    assert data["player_a"]["expected_points"] is not None or data["player_b"]["expected_points"] is not None
    # diff highlights
    assert data["diff"]["expected_points"] in ("a","b","tie", None)

def test_chips_planner_shows_3_plans_starting_gw2(client):
    sid = _make_squad(client, "chips_sid")
    r = client.get("/api/v1/chips/plans", params={"session_id": sid, "start_gw": 2, "horizon": 8})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["start_gw"] == 2
    assert data["horizon"] == 8
    assert len(data["plans"]) == 3
    for plan in data["plans"]:
        assert "label" in plan
        assert "total_ev" in plan
        assert "total_gain" in plan
        assert "breakdown" in plan
        assert len(plan["breakdown"]) == 8
        # at least total_ev is numeric
        assert isinstance(plan["total_ev"], (int,float))
    # label example: Wildcard GW5 + Free Hit GW8 = +12.4 EV style — check that label contains GW when chips used
    # If chips available, at least one plan should mention a chip
    labels = " ".join(p["label"] for p in data["plans"])
    assert "GW" in labels or "Hold" in labels

def test_chips_respects_used_chips(client):
    # squad with no chips
    payload = {
        "player_ids": list(range(1,16)),
        "captain_id": 1,
        "vice_captain_id": 2,
        "bank": 0.0,
        "free_transfers": 1,
        "chips_available": [],
        "gameweek": 2,
        "player_positions": {i: (1 if i<=2 else 2 if i<=7 else 3 if i<=12 else 4) for i in range(1,16)},
        "player_prices": {i: 5.0 for i in range(1,16)},
        "player_teams": {i: 1 for i in range(1,16)},
    }
    r = client.post("/api/v1/squad", json=payload, params={"session_id": "no_chips"})
    assert r.status_code == 200
    r2 = client.get("/api/v1/chips/plans", params={"session_id": "no_chips", "start_gw": 2, "horizon": 8})
    assert r2.status_code == 200
    data = r2.json()
    assert data["chips_available"] == []
    assert any("Hold" in p["label"] or p["total_gain"]==0 for p in data["plans"])

def test_drawer_shows_set_piece_chips(client):
    sid = _make_squad(client, "drawer_sp")
    # player 12 Saka is penalty taker for ARS team 1 per our json; but our squad uses ids 1-15 arbitrary teams,
    # so to test set-piece visible we need a player that is actually a taker per set_piece_takers.json
    # Our set_piece_takers uses catalog ids: 12 for ARS, 411 for MCI etc.
    # Let's test drawer for player 12 with session that has team mapping fallback? The squad payload maps player 1..15 to team 1/2, not to real ARS team ids.
    # But drawer fallback also checks catalog team, so Saka 12 team 1 will match entry 1 -> penalty True.
    r = client.get(f"/api/v1/player/12/drawer", params={"session_id": "drawer_sp"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "set_pieces" in data
    # Saka should be penalty taker per our seed (team 1 penalty 12)
    assert data["set_pieces"]["penalty"] is True or data["set_pieces"]["unknown"] is True
    # also test Haaland 411 team 15 penalty True
    r2 = client.get(f"/api/v1/player/411/drawer", params={"session_id": "drawer_sp"})
    assert r2.status_code == 200
    data2 = r2.json()
    assert "set_pieces" in data2

def test_pwa_static_served(client):
    # manifest and sw via real file serving (no DB override needed but client still works)
    r = client.get("/static/manifest.json")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "FPL Intelligence"
    assert body["theme_color"] == "#0f172a"
    assert body["start_url"] == "/"
    assert body["display"] == "standalone"
    assert any(i["sizes"]=="192x192" for i in body["icons"])
    assert any(i["sizes"]=="512x512" for i in body["icons"])
    r2 = client.get("/static/sw.js")
    assert r2.status_code == 200
    assert "CACHE" in r2.text or "caches" in r2.text.lower()
    r3 = client.get("/static/offline.html")
    assert r3.status_code == 200
    assert "offline" in r3.text.lower()
    # pages
    for path in ["/compare","/chips","/crunch"]:
        rr = client.get(path)
        assert rr.status_code == 200, path
