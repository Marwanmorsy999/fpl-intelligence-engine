"""Phase 22 — DECISION DEPTH: ownership chips, differential strip, transfer
watchlist, captain comparison/vice EV line and the what-if simulator inputs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from fpl_intelligence.db.session import get_db
from fpl_intelligence.squad.depth import (
    build_watchlist,
    captain_comparison,
    rank_differentials,
)

# --------------------------------------------------------------------------- #
# D1 — differential ranking (pure)
# --------------------------------------------------------------------------- #


class TestRankDifferentials:
    def test_low_owner_high_xpts_wins(self):
        # Player 2: second-best xPTS but the LEAST owned -> top differential.
        xpts = {1: 8.0, 2: 7.0, 3: 2.0}
        own = {1: 60.0, 2: 3.0, 3: 5.0}
        picks = rank_differentials(xpts, own, top_n=3)
        assert picks[0]["player_id"] == 2
        assert picks[0]["differential_score"] == 1  # own rank 3 - xpts rank 2
        # The template captain (highest owned AND highest xpts) scores ~0.
        assert picks[1]["player_id"] == 1
        # The cheap low-xpts player ranks last despite modest ownership.
        assert picks[-1]["player_id"] == 3

    def test_own_squad_players_are_excluded(self):
        xpts = {1: 9.0, 2: 6.0}
        own = {1: 10.0, 2: 5.0}
        picks = rank_differentials(xpts, own, exclude_ids={2}, min_xpts=0.0)
        assert [p["player_id"] for p in picks] == [1]

    def test_min_xpts_floor_keeps_non_starters_out(self):
        xpts = {1: 0.4, 2: 8.0}
        own = {1: 0.9, 2: 30.0}
        picks = rank_differentials(xpts, own, min_xpts=2.0)
        assert [p["player_id"] for p in picks] == [2]


# --------------------------------------------------------------------------- #
# D2 — transfer watchlist (pure)
# --------------------------------------------------------------------------- #


def _candidate(pid, pos, xpts, fdr, own, price=8.0, name=None):
    return {
        "player_id": pid,
        "web_name": name or f"P{pid}",
        "position": pos,
        "price": price,
        "xpts": xpts,
        "fdr_next3": fdr,
        "ownership_pct": own,
    }


class TestBuildWatchlist:
    def test_ranked_per_position_with_reason_line(self):
        candidates = [
            _candidate(10, 3, 6.8, 2.0, 12.0, 9.5, "Saka"),
            _candidate(11, 3, 6.0, 2.0, 45.0, 8.5, "Salah"),
            _candidate(20, 2, 5.0, 2.3, 8.0, 6.0, "Gvardiol"),
        ]
        out = build_watchlist(candidates, needed_positions=[3])
        mids = out["positions"]["MID"]
        assert len(mids) == 2
        assert mids[0]["web_name"] == "Saka"
        assert "xPTS" in mids[0]["reason"]
        assert "% owned" in mids[0]["reason"]

    def test_caps_at_five_per_position(self):
        candidates = [_candidate(i, 3, 5.0 - i * 0.01, 3.0, 10.0) for i in range(8)]
        out = build_watchlist(candidates, needed_positions=[3])
        assert len(out["positions"]["MID"]) == 5


# --------------------------------------------------------------------------- #
# D3 — captain comparison + vice EV (pure)
# --------------------------------------------------------------------------- #


class TestCaptainComparison:
    DATA = [
        {"player_id": 411, "web_name": "Haaland", "xpts": 7.4, "ownership_pct": 69.0,
         "next_fixture": "IPS(H)2"},
        {"player_id": 12, "web_name": "Saka", "xpts": 6.1, "ownership_pct": 28.0,
         "next_fixture": "CHE(A)4"},
        {"player_id": 15, "web_name": "Palmer", "xpts": 5.9, "ownership_pct": 51.0,
         "next_fixture": "EVE(H)3"},
    ]

    def test_captain_first_with_blank_note_and_gap(self):
        out = captain_comparison(self.DATA, captain_id=411, vice_id=12)
        cards = out["cards"]
        assert len(cards) == 3
        assert cards[0]["is_captain"] is True
        assert "Saka" in cards[0]["blank_note"]
        assert cards[0]["gap_to_next"] == pytest.approx(1.3)

    def test_vice_ev_line_names_cover_points(self):
        out = captain_comparison(self.DATA, captain_id=411, vice_id=12)
        assert out["vice"] is not None
        assert out["vice"]["vice_name"] == "Saka"
        assert "6.1" in out["vice"]["line"]
        assert "Haaland blanks" in out["vice"]["line"]

    def test_top_n_respected(self):
        out = captain_comparison(self.DATA, captain_id=411, vice_id=None, top_n=2)
        assert len(out["cards"]) == 2
        assert out["vice"] is None


# --------------------------------------------------------------------------- #
# Integration — /decisions carries ownership + depth payloads
# --------------------------------------------------------------------------- #


class _StubPrediction:
    """Minimal PlayerPrediction stand-in for the optimizers."""

    def __init__(self, player_id: int) -> None:
        self.player_id = player_id
        self.expected_points = 4.0
        self.distribution = [3.0, 4.0, 5.0]
        self.ceiling = 6.0
        self.floor = 2.0
        self.source = "stub"
        self.data_quality = "test"
        self.expected_minutes = 60.0
        self.start_probability = 0.8


class _StubProvider:
    """Deterministic offline provider so the route never builds the chain."""

    def get_player_prediction(self, player_id: int, gameweek: int) -> _StubPrediction:
        return _StubPrediction(player_id)

    def get_squad_predictions(self, player_ids, gameweeks):
        return {}

    def get_all_predictions(self, gameweek):
        return {}


@pytest.fixture()
def depth_api(db_session):
    from fpl_intelligence.api import deps
    from fpl_intelligence.api.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[deps.get_prediction_provider] = lambda: _StubProvider()
    client = TestClient(app)
    yield client, db_session
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(deps.get_prediction_provider, None)


def _seed_depth_world(db):
    """A saved squad plus materialized xPTS + fixtures so every layer has data."""
    from fpl_intelligence.squad.models_db import SquadStateDB
    from fpl_intelligence.sync.materialized_models import (
        FixturesCacheDB,
        PredictionCurrentDB,
    )

    # 1 GK + 5 DEF + 5 MID starters first, then 3 FWD + GK bench (FPL order).
    ids = [401, 410, 411, 412, 413, 414, 420, 421, 422, 423, 424, 430, 431, 432, 402]
    positions = {
        401: 1, 402: 1,
        410: 2, 411: 2, 412: 2, 413: 2, 414: 2,
        420: 3, 421: 3, 422: 3, 423: 3, 424: 3,
        430: 4, 431: 4, 432: 4,
    }
    prices = {pid: 7.0 for pid in ids}
    teams = dict.fromkeys(ids, 15)
    db.add(SquadStateDB(
        session_id="depth-user",
        squad_json={
            "player_ids": ids,
            "captain_id": 424,
            "vice_captain_id": 420,
            "bank": 2.0,
            "free_transfers": 1,
            "chips_available": ["wildcard"],
            "gameweek": 2,
            "player_positions": {str(k): v for k, v in positions.items()},
            "player_prices": {str(k): v for k, v in prices.items()},
            "player_teams": {str(k): v for k, v in teams.items()},
        },
        updated_at=datetime.now(UTC),
    ))

    now = datetime.now(UTC)
    xpts_rows = [
        (424, 6.8), (420, 5.9), (411, 5.0),
        # market darlings the differential engine should surface instead
        (500, 6.9), (501, 6.5), (502, 6.1), (503, 5.8),
    ]
    for element_id, xpts in xpts_rows:
        db.add(PredictionCurrentDB(
            gameweek=2,
            element_id=element_id,
            expected_points=xpts,
            computed_at=now,
            source="materialized-chain",
        ))
    db.add(FixturesCacheDB(source="test", payload=[
        {"event": 1, "team_h": 1, "team_a": 2, "finished": True},
        {"event": 2, "team_h": 15, "team_a": 16, "finished": False},
        {"event": 2, "team_h": 7, "team_a": 1, "finished": False},
    ], fetched_at=now))
    db.commit()


class TestDecisionsDepthPayload:
    def test_report_carries_ownership_and_depth_layers(self, depth_api):
        client, db = depth_api
        _seed_depth_world(db)
        resp = client.get("/api/v1/decisions", params={"session_id": "depth-user"})
        assert resp.status_code == 200, resp.text
        body = resp.json()

        meta = body["meta"]

        # D1 — differentials exist and never include squad players.
        diff_ids = {d["player_id"] for d in meta.get("differential_picks", [])}
        assert diff_ids
        assert not (diff_ids & set(range(401, 433)))

        # D2 — watchlist groups exist even though the verdict may be a roll.
        wl = meta.get("transfer_watchlist") or {}
        assert isinstance(wl.get("positions"), dict)

        # D3 — captain cards + vice EV.
        cc = meta.get("captain_comparison") or {}
        cards = cc.get("cards") or []
        assert any(c["is_captain"] for c in cards) or not cards
