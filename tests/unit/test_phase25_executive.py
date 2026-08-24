"""Phase 25 — executive intelligence tests.

Gate 0 (data & math):
* T1 transfer ledger: official parsing, snapshot-diff fallback, horizon EV.
* T2 alpha engine: pure math (pos_avg, ownership, alpha, volatility, need
  weights) plus the /targets endpoint contract with honest unavailable chips.
* T3 planner: price-pressure fallback + plan text + endpoint contract.

Regression guard (both gates): the pre-existing payload shapes of /decisions,
/league, /live, /track-record, /api/v1/sync/calibration and the player drawer
must stay byte-compatible in structure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# T1 — transfers
# --------------------------------------------------------------------------- #


def test_parse_history_transfers_flattens_and_skips_bad_rows() -> None:
    from fpl_intelligence.transfers.service import parse_history_transfers

    payload = {
        "history": [
            {
                "event": 3,
                "transfers": [
                    {"id": 11, "element_in": 411, "element_out": 4, "event_cost": 4},
                    {"id": 12, "element_in": 399, "element_out": 7, "event_cost": 0},
                    {"id": None, "element_in": None, "element_out": None},  # skipped
                ],
            }
        ]
    }
    rows = parse_history_transfers(payload)
    assert len(rows) == 2
    assert rows[0] == {
        "gameweek": 3,
        "transfer_id": 11,
        "element_in": 411,
        "element_out": 4,
        "cost": 4,
    }


def test_snapshot_diff_rows_pairs_leavers_and_joiners(db_session) -> None:
    from fpl_intelligence.transfers.models import SquadSnapshotDB
    from fpl_intelligence.transfers.service import snapshot_diff_rows

    now = datetime.now(UTC)
    db_session.add(
        SquadSnapshotDB(
            entry_id="2295006",
            gameweek=2,
            player_ids=[1, 2, 3, 4],
            captured_at=now,
        )
    )
    db_session.add(
        SquadSnapshotDB(
            entry_id="2295006",
            gameweek=3,
            player_ids=[1, 9, 10, 11],
            captured_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    rows = snapshot_diff_rows(db_session, "2295006")
    assert len(rows) == 3
    ins = {r["element_in"] for r in rows}
    outs = {r["element_out"] for r in rows}
    assert ins == {9, 10, 11}
    assert outs == {2, 3, 4}


def test_compute_horizon_ev_uses_materialized_predictions_only(db_session) -> None:
    from fpl_intelligence.sync.materialized_models import PredictionCurrentDB
    from fpl_intelligence.transfers.service import compute_horizon_ev

    now = datetime.now(UTC)
    for gw, eid, pts in ((2, 411, 6.0), (3, 411, 5.0), (2, 4, 2.0)):
        db_session.add(
            PredictionCurrentDB(
                gameweek=gw,
                element_id=eid,
                expected_points=pts,
                computed_at=now,
            )
        )
    db_session.commit()

    rows = compute_horizon_ev(
        db_session,
        [{"gameweek": 3, "transfer_id": 1, "element_in": 411, "element_out": 4, "cost": 4}],
        start_gw=2,
    )
    row = rows[0]
    # GW2 contributes 6.0 - 2.0 = 4.0; GW3 has no OUT prediction -> excluded gap.
    assert row["horizon_ev"] == pytest.approx(0.0)  # 4.0 - 4 hit cost
    assert 2 in row["horizon_gws"] and 3 not in row["horizon_gws"]


@pytest.mark.anyio
async def test_build_ledger_unavailable_is_honest(db_session, monkeypatch) -> None:
    from fpl_intelligence.transfers import service as ts

    async def _fail(entry_id: int):
        raise RuntimeError("all masks down")

    monkeypatch.setattr(ts, "fetch_official_transfers", _fail)
    payload = await ts.build_ledger(db_session, "777")
    assert payload["status"] == "unavailable"
    assert payload["transfers"] == []
    assert payload["count"] == 0


def test_capture_snapshot_dedupes_identical_rosters(db_session) -> None:
    from fpl_intelligence.transfers.models import SquadSnapshotDB
    from fpl_intelligence.transfers.service import capture_snapshot

    first = capture_snapshot(db_session, "42", [1, 2, 3], 1, 0.5)
    second = capture_snapshot(db_session, "42", [1, 2, 3], 2, 0.5)
    third = capture_snapshot(db_session, "42", [1, 2, 4], 3, 0.5)
    assert first is True and second is False and third is True
    count = len(
        db_session.query(SquadSnapshotDB).filter(SquadSnapshotDB.entry_id == "42").all()
    )
    assert count == 2


def test_squad_save_captures_snapshot(db_session) -> None:
    from sqlalchemy import select

    from fpl_intelligence.squad.models import SquadStateCreate
    from fpl_intelligence.squad.service import SquadService
    from fpl_intelligence.transfers.models import SquadSnapshotDB

    svc = SquadService(session=db_session)
    svc.set_squad(
        SquadStateCreate(
            player_ids=list(range(1, 16)),
            captain_id=1,
            vice_captain_id=2,
            bank=1.0,
            free_transfers=1,
            gameweek=2,
            session_id="999",
        ),
        session_id="999",
    )
    snap = db_session.scalar(
        select(SquadSnapshotDB).where(SquadSnapshotDB.entry_id == "999")
    )
    assert snap is not None
    assert len(snap.player_ids) == 15


# --------------------------------------------------------------------------- #
# T2 — alpha engine math
# --------------------------------------------------------------------------- #


def test_position_average_groups_by_position() -> None:
    from fpl_intelligence.alpha.service import position_average

    avg = position_average({1: 6.0, 2: 4.0, 3: 8.0}, {1: 3, 2: 3, 3: 4})
    assert avg[3] == pytest.approx(5.0)
    assert avg[4] == pytest.approx(8.0)


def test_league_ownership_uses_rivals_when_thin_falls_back_to_global() -> None:
    from fpl_intelligence.alpha.service import MIN_LEAGUE_RIVALS, league_ownership

    picks = {str(i): [411] for i in range(MIN_LEAGUE_RIVALS)}
    own, label = league_ownership(411, picks, "55.5")
    assert own == pytest.approx(1.0) and label == "league ownership"

    own2, label2 = league_ownership(411, {}, "55.5%")
    assert own2 == pytest.approx(0.555)
    assert label2 == "global ownership (league data thin)"

    own3, label3 = league_ownership(411, {}, None)
    assert own3 is None and label3 == "unavailable"


def test_alpha_score_shows_both_terms_and_hides_without_ownership() -> None:
    from fpl_intelligence.alpha.service import alpha_score

    alpha, terms = alpha_score(8.0, 5.0, 0.25)
    assert terms["edge"] == pytest.approx(3.0)
    assert alpha == pytest.approx(2.25)

    alpha_none, terms2 = alpha_score(8.0, 5.0, None)
    assert alpha_none is None
    assert terms2["own_p"] is None


def test_recent_volatility_needs_two_samples() -> None:
    from fpl_intelligence.alpha.service import recent_volatility

    assert recent_volatility([5]) is None
    assert recent_volatility([4, 4, 4]) == 0.0
    assert recent_volatility([2, 8]) == 3.0


def test_need_boost_boosts_thinnest_position() -> None:
    from fpl_intelligence.alpha.service import position_need_boost

    weights = position_need_boost({1: 2, 2: 5, 3: 5, 4: 3})
    assert weights[1] > weights[4] > weights[2]
    assert max(weights.values()) <= 1.2001


# --------------------------------------------------------------------------- #
# Endpoint contracts (TestClient on sqlite; egress stubbed off)
# --------------------------------------------------------------------------- #


class _NoNetworkChain:
    """Stand-in that always fails, exercising honest fallbacks."""

    winning_strategy = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def fetch(self, *args: Any, **kwargs: Any):  # noqa: ANN201
        raise RuntimeError("network disabled in tests")


@pytest.fixture()
def api_client(db_session, monkeypatch):
    from fastapi.testclient import TestClient

    from fpl_intelligence.api.main import app
    from fpl_intelligence.data_providers.fpl_egress import FplEgressChain
    from fpl_intelligence.db.session import get_db

    monkeypatch.setattr(FplEgressChain, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(FplEgressChain, "fetch", _NoNetworkChain.fetch)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    yield client, db_session
    app.dependency_overrides.pop(get_db, None)


def test_targets_endpoint_contract(api_client) -> None:
    client, db = api_client
    resp = client.get("/api/v1/targets")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("gameweek", "targets", "next_gw_focus", "position_avgs"):
        assert key in body
    if body["targets"]:
        t = body["targets"][0]
        for key in (
            "player_id", "web_name", "price", "xpts", "pos_avg", "edge",
            "own_p", "ownership_label", "alpha", "volatility",
            "fixture_strip", "reason", "affordability", "how_computed",
        ):
            assert key in t


def test_planner_requires_saved_squad(api_client) -> None:
    client, _db = api_client
    resp = client.get("/api/v1/planner?session_id=nobody")
    assert resp.status_code == 200
    assert resp.json()["status"] == "no-squad"


def test_transfers_ledger_numeric_guard(api_client) -> None:
    client, _db = api_client
    resp = client.get("/api/v1/transfers/ledger?entry_id=abc")
    assert resp.status_code == 200
    assert resp.json()["status"] == "unavailable"


def test_plan_text_contains_sections() -> None:
    from fpl_intelligence.planner.service import build_plan_text

    text = build_plan_text(
        {
            "generated_at": "t",
            "gameweek": 2,
            "plan_steps": [{"gameweek": 2, "action": "buy A out B", "ev": 1.5}],
            "assumptions": ["bank £0.0m"],
            "price_pressure": {"pressure": "low", "inputs": "x"},
            "how_computed": "alpha",
        }
    )
    assert "PLAN" in text and "ASSUMPTIONS" in text and "RISE PRESSURE" in text
    assert "GW2: buy A out B" in text


# --------------------------------------------------------------------------- #
# Regression guard — existing payload shapes stay intact
# --------------------------------------------------------------------------- #


def _seed_squad(db_session, key: str = "5001") -> None:
    from fpl_intelligence.squad.models import SquadStateCreate
    from fpl_intelligence.squad.service import SquadService

    extra_ids = list(range(20, 32))
    positions = {411: 3, 4: 3, 399: 4}
    for i, pid in enumerate(extra_ids):
        # 2 GK + 5 DEF + 5 MID + 3 FWD shape so the XI optimizer has a legal pool.
        positions[pid] = 1 if i < 2 else (2 if i < 7 else (3 if i < 12 else 4))
    SquadService(session=db_session).set_squad(
        SquadStateCreate(
            player_ids=[411, 4, 399] + extra_ids,
            captain_id=411,
            vice_captain_id=4,
            bank=1.5,
            free_transfers=2,
            chips_available=["wildcard"],
            gameweek=2,
            player_positions=positions,
            player_prices={**{411: 10.5, 4: 5.5, 399: 8.0},
                           **{pid: 5.0 for pid in extra_ids}},
            player_teams={**{411: 1, 4: 2, 399: 3}, **{pid: 1 for pid in extra_ids}},
            session_id=key,
        ),
        session_id=key,
    )


def test_regression_decisions_shape(api_client) -> None:
    client, db = api_client
    _seed_squad(db)
    resp = client.get("/api/v1/decisions?session_id=5001")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("gameweek", "starting_xi", "bench_order", "captain", "players", "meta"):
        assert key in body
    assert "squad_summary" in body["meta"]
    summary = body["meta"]["squad_summary"]
    for key in ("team_value", "bank", "free_transfers", "chips_available"):
        assert key in summary


def test_regression_drawer_shape(api_client) -> None:
    client, db = api_client
    _seed_squad(db)
    resp = client.get("/api/v1/player/411/drawer?session_id=5001")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "player", "expected_points", "form_bars", "fixture_runs",
        "degraded", "missing", "aliases", "set_pieces",
    ):
        assert key in body
    assert resp.json()["player"]["id"] == 411


def test_regression_sync_calibration_shape(api_client) -> None:
    client, _db = api_client
    resp = client.get("/api/v1/sync/calibration")
    assert resp.status_code == 200
    body = resp.json()
    assert "mae" in body and "bias" in body and "count" in body


def test_regression_track_record_shape(api_client) -> None:
    client, _db = api_client
    resp = client.get("/api/v1/sync/track-record?entry_id=5001")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("cards", "rolling"):
        assert key in body


def test_regression_live_board_404_without_squad(api_client) -> None:
    client, _db = api_client
    resp = client.get("/api/v1/live-board?session_id=ghost")
    assert resp.status_code == 404
