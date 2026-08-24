"""Phase 24 — compare + chips + set pieces smoke."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.live_intelligence.bridge import StaticPredictionProvider


@pytest.fixture
def client(db_session: Session):
    from sqlalchemy import delete

    from fpl_intelligence.api.main import app
    from fpl_intelligence.squad.models_db import SquadStateDB
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
    # 5 known takers from set_piece_takers.json: Saka 12 (ARS), Haaland 411 (MCI), Palmer 154 (CHE), Watkins 55 (AVL), Isak 379 (LIV)
    for pid, should_have in [(12, "penalty"), (411, "penalty"), (154, "penalty"), (55, "penalty"), (379, "penalty")]:
        r = client.get(f"/api/v1/player/{pid}/drawer", params={"session_id": "drawer_sp"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "set_pieces" in data, f"missing set_pieces for {pid}"
        # each of these 5 is a designated taker, so at least one flag true
        flags = data["set_pieces"]
        assert any(flags.get(k) is True for k in ("penalty","corners","free_kicks")), f"{pid} should be taker"
    # also test unknown handling for unmapped? our json covers all 20, but unknown path still exists
    r2 = client.get("/api/v1/player/1/drawer", params={"session_id": "drawer_sp"})
    assert r2.status_code == 200
    assert "set_pieces" in r2.json()

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
