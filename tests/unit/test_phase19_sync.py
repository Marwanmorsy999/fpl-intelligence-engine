"""Phase 19.0 — sync trio, track-record scoring and calibration unit tests.

Covers the required artifacts:
* push endpoint auth (token unset -> 503, wrong bearer -> 401, right -> 200),
* bookmarklet payload parsing (squad-push converts picks -> saved squad),
* track-record scoring math (captain / transfer / xi / hit-rate),
* calibration compute (mae/bias/buckets),
* history-push math updates (baseline rebuilds with through_gw advanced),
* live-board aggregation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fpl_intelligence.db.session import get_db
from fpl_intelligence.sync.scoring import (
    RIGHT,
    WRONG,
    compute_calibration,
    rolling_hit_rate,
    score_captain,
    score_transfer,
    score_xi,
)


@pytest.fixture()
def api(db_session, monkeypatch):
    """TestClient wired to an in-memory sqlite session + SYNC_PUSH_TOKEN set."""
    from fpl_intelligence.api.main import app
    from fpl_intelligence.config import get_settings

    token_value = "tok-" + "a" * 32
    monkeypatch.setattr(get_settings(), "sync_push_token", token_value)

    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    yield client, db_session, token_value
    app.dependency_overrides.pop(get_db, None)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


SQUAD_PUSH_BODY = {
    "entry_id": 794561,
    "entry_name": "Phase19 FC",
    "gameweek": 3,
    "bank": 1.5,
    "transfers": {"limit": 2, "made": 1},
    "picks": [
        {"element_id": 100 + i, "position": i + 1, "is_captain": i == 0, "is_vice": i == 1}
        for i in range(15)
    ],
}


PUSH_PATHS = [
    "/api/v1/sync/squad-push",
    "/api/v1/sync/live-push",
    "/api/v1/sync/history-push",
]
FPL_ORIGIN = {"Origin": "https://fantasy.premierleague.com"}


class TestBookmarkletCors:
    """Phase 19.1 — the bookmarklet POSTs cross-origin from fantasy.premierleague.com."""

    @pytest.mark.parametrize("path", PUSH_PATHS)
    def test_preflight_options_is_204_with_cors_headers(self, api, path):
        client, _db, _token = api
        resp = client.options(
            path,
            headers={
                **FPL_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
        assert resp.status_code == 204, resp.text
        assert resp.headers["access-control-allow-origin"] == "*"
        assert resp.headers["access-control-allow-methods"] == "POST, OPTIONS"
        assert resp.headers["access-control-allow-headers"] == "Authorization, Content-Type"
        assert resp.headers["access-control-max-age"] == "86400"
        # Preflight must answer before any auth check — browsers send it bare.
        assert resp.content == b""

    def test_post_response_carries_allow_origin_star(self, api):
        client, _db, token = api
        resp = client.post(
            "/api/v1/sync/squad-push",
            json=SQUAD_PUSH_BODY,
            headers={**_bearer(token), **FPL_ORIGIN},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["access-control-allow-origin"] == "*"

    def test_post_without_token_still_401(self, api):
        client, _db, _token = api
        resp = client.post(
            "/api/v1/sync/squad-push", json=SQUAD_PUSH_BODY, headers=dict(FPL_ORIGIN)
        )
        assert resp.status_code == 401

    def test_non_push_routes_untouched(self, api):
        client, _db, token = api
        client.post("/api/v1/sync/live-push", json={
            "gameweek": 2, "elements": [{"element_id": 1, "points": 0}]
        }, headers=_bearer(token))
        resp = client.get("/api/v1/sync/status")
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers


class TestPushAuth:
    def test_missing_token_config_rejects_with_503(self, api, monkeypatch):
        client, db, _token = api
        from fpl_intelligence.config import get_settings

        monkeypatch.setattr(get_settings(), "sync_push_token", "")
        resp = client.post(
            "/api/v1/sync/squad-push", json=SQUAD_PUSH_BODY, headers=_bearer("anything")
        )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"].lower()

    def test_wrong_and_missing_bearer_are_401(self, api):
        client, _db, token = api
        wrong = client.post(
            "/api/v1/sync/squad-push", json=SQUAD_PUSH_BODY, headers=_bearer("nope")
        )
        assert wrong.status_code == 401
        none = client.post("/api/v1/sync/squad-push", json=SQUAD_PUSH_BODY)
        assert none.status_code == 401

    def test_correct_bearer_squad_push_persists_under_entry_key(self, api):
        client, db, token = api
        resp = client.post(
            "/api/v1/sync/squad-push", json=SQUAD_PUSH_BODY, headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["session_id"] == "794561"
        # The squad is retrievable through the normal per-session read path.
        got = client.get("/api/v1/squad", params={"session_id": "794561"})
        assert got.status_code == 200
        squad = got.json()
        assert len(squad["player_ids"]) == 15
        assert squad["captain_id"] == 100
        # free transfers derived from transfers payload (2 - 1)
        assert squad["free_transfers"] == 1

    def test_live_push_upserts(self, api):
        client, db, token = api
        payload = {
            "gameweek": 3,
            "elements": [{"element_id": 100, "points": 2, "minutes": 45}],
        }
        first = client.post("/api/v1/sync/live-push", json=payload, headers=_bearer(token))
        assert first.status_code == 200 and first.json()["saved"] == 1
        second = client.post(
            "/api/v1/sync/live-push",
            json={**payload, "elements": [{"element_id": 100, "points": 5}]},
            headers=_bearer(token),
        )
        assert second.status_code == 200 and second.json()["updated"] == 1

    def test_history_push_auth_gated_too(self, api):
        client, _db, token = api
        bad = client.post(
            "/api/v1/sync/history-push",
            json={"gameweek": 3, "elements": [{"element_id": 1, "total_points": 2}]},
            headers=_bearer("wrong"),
        )
        assert bad.status_code == 401


class TestBookmarkletPayloadParsing:
    def test_captain_flag_required(self, api):
        client, _db, token = api
        body = {**SQUAD_PUSH_BODY, "picks": [
            {"element_id": 200 + i, "position": i + 1} for i in range(15)
        ]}
        resp = client.post("/api/v1/sync/squad-push", json=body, headers=_bearer(token))
        assert resp.status_code == 422
        assert "is_captain" in resp.json()["detail"]

    def test_fifteen_picks_enforced(self, api):
        client, _db, token = api
        body = {**SQUAD_PUSH_BODY, "picks": SQUAD_PUSH_BODY["picks"][:10]}
        resp = client.post("/api/v1/sync/squad-push", json=body, headers=_bearer(token))
        assert resp.status_code == 422

    def test_element_type_rides_through_to_positions(self, api):
        client, db, token = api
        picks = [
            {"element_id": 300 + i, "position": i + 1, "element_type": 1 if i < 2 else 4,
             "is_captain": i == 0, "is_vice": i == 1}
            for i in range(15)
        ]
        resp = client.post(
            "/api/v1/sync/squad-push",
            json={**SQUAD_PUSH_BODY, "entry_id": 555001, "picks": picks},
            headers=_bearer(token),
        )
        assert resp.status_code == 200
        squad = client.get("/api/v1/squad", params={"session_id": "555001"}).json()
        assert squad["player_positions"]["300"] == 1
        assert squad["player_positions"]["302"] == 4


class TestTrackRecordScoringMath:
    def test_captain_right_when_outscored_best_alternative(self):
        actual = {10: 12, 11: 8, 12: 3}
        s = score_captain(10, [11, 12], actual)
        assert s is not None and s["verdict"] == RIGHT
        assert s["delta"] == (12 - 8) * 2

    def test_captain_wrong_doubled(self):
        actual = {10: 2, 11: 9}
        s = score_captain(10, [11], actual)
        assert s["verdict"] == WRONG
        assert s["delta"] == (2 - 9) * 2

    def test_captain_tie_is_neutral(self):
        actual = {10: 6, 11: 6}
        s = score_captain(10, [11], actual)
        assert s["verdict"] == "neutral" and s["delta"] == 0

    def test_transfer_subtracts_hit_cost(self):
        s = score_transfer([21], [22], {21: 7, 22: 5}, hit_cost=4)
        assert s["delta"] == -2 and s["verdict"] == WRONG
        s_free = score_transfer([21], [22], {21: 7, 22: 5}, hit_cost=0)
        assert s_free["verdict"] == RIGHT

    def test_xi_identical_is_neutral_with_reason(self):
        # Phase 23 (C2): identical XIs grade NEUTRAL with a stated reason —
        # never None, so the row cannot sit pending once results exist.
        xi = list(range(1, 12))
        actual = {pid: 3 for pid in range(1, 16)}
        s = score_xi(xi, xi, actual)
        assert s is not None
        assert s["verdict"] == "neutral"
        assert s["delta"] == 0
        assert "matched your fielded XI" in s["reason"]

    def test_xi_delta_positive(self):
        rec = [1, 2] + list(range(3, 12))
        user = [1, 15] + list(range(3, 12))
        actual = {pid: 2 for pid in range(1, 16)}
        actual[2], actual[15] = 9, 1
        s = score_xi(rec, user, actual)
        assert s["delta"] == 8 and s["verdict"] == RIGHT

    def test_missing_actual_is_unscoreable(self):
        assert score_captain(10, [11], {10: 5}) is None
        assert score_transfer([21], [22], {21: 5}) is None

    def test_rolling_hit_rate_aggregates(self):
        scores = [
            {"verdict": RIGHT, "delta": 4},
            {"verdict": WRONG, "delta": -6},
            {"verdict": "neutral", "delta": 0},
            {"verdict": WRONG, "delta": -2},
            {"verdict": RIGHT, "delta": 1},
            {"verdict": RIGHT, "delta": 3},
        ]
        agg = rolling_hit_rate(scores)
        assert agg["graded"] == 6 and agg["hits"] == 4
        assert agg["hit_rate"] == round(4 / 6, 3)
        assert agg["net_points"] == 0
        assert len(agg["last_5"]) == 5


class TestCalibrationCompute:
    def test_empty_ledger_reports_zero_counts(self):
        cal = compute_calibration([])
        assert cal == {"count": 0, "mae": None, "bias": None, "buckets": {}}

    def test_mae_bias_and_buckets(self):
        rows = [(5.0, 5), (6.0, 3), (10.0, 2), (2.0, 8)]
        cal = compute_calibration(rows)
        errors = [0.0, 3.0, 8.0, -6.0]
        assert cal["count"] == 4
        assert cal["mae"] == round(sum(abs(e) for e in errors) / 4, 3)
        assert cal["bias"] == round(sum(errors) / 4, 3)
        assert cal["buckets"]["<2"]["count"] == 1
        assert cal["buckets"]["2-5"]["count"] == 1
        assert cal["buckets"]["5-10"]["count"] == 2
        assert cal["buckets"][">10"]["count"] == 0


class TestHistoryPushMathUpdates:
    @pytest.fixture()
    def seeded_players(self, populated_db):
        """Give the conftest players real FPL element ids + a GW row."""
        from fpl_intelligence.db.models import Player

        players = populated_db.execute(select(Player)).scalars().all()
        for i, p in enumerate(players, start=1):
            p.fpl_element_id = 400 + i
        populated_db.flush()
        return populated_db, [p.fpl_element_id for p in players]

    def test_ingesting_new_gw_advances_baseline_through_label(self, seeded_players):
        db, elements = seeded_players
        from fpl_intelligence.prediction.live_provider import _baseline_points_for_gameweek
        from fpl_intelligence.sync.service import ingest_history_gameweek

        # Before ingestion there is no GW3 data; baseline for gw4 sees nothing new.
        before = _baseline_points_for_gameweek(db, 4)
        through_before = before.notes.get("through_gw") if before else None

        summary = ingest_history_gameweek(
            db, 3,
            [
                {"element_id": eid, "total_points": 7 + idx, "minutes": 90, "bonus": 1}
                for idx, eid in enumerate(elements)
            ],
            source="unit-test",
        )
        assert summary["stored"] == len(elements)
        assert summary["mirrored"] >= 1
        assert summary["gameweek"] == 3

        after = _baseline_points_for_gameweek(db, 4)
        assert after is not None
        assert after.notes["through_gw"] == 3
        if through_before is not None:
            assert through_before < after.notes["through_gw"]

    def test_prediction_ledger_captures_pre_match_forecast_then_reconciles(self, seeded_players):
        db, elements = seeded_players
        from fpl_intelligence.db.models import Gameweek, PlayerGameweekPerformance
        from fpl_intelligence.sync.models import PredictionLedgerDB
        from fpl_intelligence.sync.service import (
            capture_pre_ingest_predictions,
            ingest_history_gameweek,
        )

        captured = capture_pre_ingest_predictions(db, 3)
        # Baseline needs >=25% universe coverage; 4 players may fall short —
        # accept either outcome but never a crash, and verify reconciliation.
        ingest_summary = ingest_history_gameweek(
            db, 3,
            [{"element_id": e, "total_points": 5, "minutes": 90} for e in elements],
        )

        rows = db.execute(
            select(PredictionLedgerDB).where(PredictionLedgerDB.gameweek == 3)
        ).scalars().all()
        if captured:
            assert all(r.actual == 5 for r in rows)
            assert all(r.reconciled_at is not None for r in rows)
            assert ingest_summary["calibration"]["count"] >= 1
        else:
            assert rows == []

        # The mirrored performance rows exist for the ingested GW.
        gw_row = db.scalar(select(Gameweek).where(Gameweek.provider_event_id == 3))
        perf_count = len(db.execute(
            select(PlayerGameweekPerformance).where(
                PlayerGameweekPerformance.gameweek_id == gw_row.id
            )
        ).all())
        assert perf_count >= 1

    def test_recommendations_auto_score_after_results_land(self, seeded_players):
        db, _elements = seeded_players

        from fpl_intelligence.db.models import Player
        from fpl_intelligence.sync.models import RecommendationDB
        from fpl_intelligence.sync.service import (
            ingest_history_gameweek,
            record_recommendations,
        )

        players = db.execute(
            select(Player).where(Player.fpl_element_id.is_not(None))
        ).scalars().all()
        captain_el = players[0].fpl_element_id
        alts = [p.fpl_element_id for p in players[1:3]]

        class FakeCaptain:
            player_id = captain_el
            expected_points = 6.0
            main_reason = "form"

        class FakeReport:
            gameweek = 3
            starting_xi = [captain_el] + alts
            bench_order = []
            captain = FakeCaptain()
            vice_captain = alts[0]
            transfer_plan = None
            chip_recommendation = None
            players = {}
            meta = {}

        record_recommendations(db, "777001", FakeReport())
        pending = db.execute(
            select(RecommendationDB).where(RecommendationDB.rec_type == "captain")
        ).scalars().all()
        assert len(pending) == 1 and pending[0].scored_at is None

        # Ingest results where an alternative outscored the captain -> WRONG.
        ingest_history_gameweek(
            db, 3,
            [
                {"element_id": captain_el, "total_points": 1, "minutes": 90},
                {"element_id": alts[0], "total_points": 10, "minutes": 90},
                {"element_id": alts[1], "total_points": 2, "minutes": 90},
            ],
        )
        refreshed = db.scalar(
            select(RecommendationDB).where(RecommendationDB.rec_type == "captain")
        )
        assert refreshed.scored_at is not None
        assert refreshed.score["verdict"] == WRONG
        assert refreshed.score["delta"] == (1 - 10) * 2

    def test_track_record_payload_shape(self, seeded_players):
        db, _ = seeded_players
        from fpl_intelligence.db.models import Player
        from fpl_intelligence.sync.models import RecommendationDB
        from fpl_intelligence.sync.service import track_record_payload

        player = db.execute(
            select(Player).where(Player.fpl_element_id.is_not(None))
        ).scalars().first()
        db.add(
            RecommendationDB(
                session_key="888001",
                gameweek=2,
                rec_type="captain",
                subject={"captain_id": player.fpl_element_id},
                detail={"alternatives": [], "expected_points": 5.0},
                created_at=datetime.now(UTC),
            )
        )
        db.flush()
        payload = track_record_payload(db, "888001")
        assert payload["entry_id"] == "888001"
        assert len(payload["cards"]) == 1
        assert payload["cards"][0]["scored"] is False
        assert payload["rolling"]["graded"] == 0
        assert payload["rolling"]["hit_rate"] is None


class TestSyncMigrationEndpoint:
    def test_migrate_sync_tables_applies_then_seals(self, monkeypatch):
        """One-shot DDL hotfix: creates missing tables, then answers 410."""
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from fpl_intelligence.api import routes
        from fpl_intelligence.api.main import app
        from fpl_intelligence.db.base import Base
        from fpl_intelligence.db.models import (
            IngestionRun,  # noqa: F401
            Season,  # noqa: F401
        )

        # Register the core metadata (players/seasons/ingestion_runs) but NOT
        # the sync models, so the endpoint has real work to do.
        from fpl_intelligence.squad import models_db as _squad_models  # noqa: F401

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def _fk(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(
            engine,
            tables=[
                t
                for t in Base.metadata.sorted_tables
                if t.name not in ("sync_live_points", "ingested_history",
                                  "recommendation", "prediction_ledger", "sync_log")
            ],
        )
        session = sessionmaker(bind=engine)()
        # The hotfix opens its own production session; point it at sqlite.
        monkeypatch.setattr(routes.admin, "SessionLocal", lambda: session)
        client = TestClient(app)
        try:
            first = client.post("/api/v1/admin/migrate-sync-tables")
            assert first.status_code == 200, first.text
            assert set(first.json()["tables_created"]) == {
                "sync_live_points", "ingested_history", "recommendation",
                "prediction_ledger", "sync_log",
            }
            second = client.post("/api/v1/admin/migrate-sync-tables")
            assert second.status_code == 410
        finally:
            session.close()


class TestLiveBoard:
    def test_live_board_honest_without_data(self, api):
        client, db, token = api
        client.post(
            "/api/v1/sync/squad-push", json=SQUAD_PUSH_BODY, headers=_bearer(token)
        )
        resp = client.get("/api/v1/sync/live-board", params={"session_id": "794561"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_live_data"] is False
        assert data["note"] and "ESPN" in data["note"]
        assert data["espn_fallback_url"].startswith("https://www.espn.com")

    def test_live_board_totals_double_the_captain(self, api):
        client, db, token = api
        client.post(
            "/api/v1/sync/squad-push", json=SQUAD_PUSH_BODY, headers=_bearer(token)
        )
        elements = [
            {"element_id": pid, "points": 3, "minutes": 60}
            for pid in range(100, 115)
        ]
        client.post(
            "/api/v1/sync/live-push",
            json={"gameweek": 3, "elements": elements},
            headers=_bearer(token),
        )
        data = client.get("/api/v1/sync/live-board", params={"session_id": "794561"}).json()
        assert data["has_live_data"] is True
        # 11 starters x 3 pts + captain doubled (+3) = 36
        assert data["total_live_points"] == 33
        assert data["effective_total"] == 36
        starters = [r for r in data["rows"] if not r["on_bench"]]
        bench = [r for r in data["rows"] if r["on_bench"]]
        assert len(starters) == 11 and len(bench) == 4

    def test_sync_status_public_read(self, api):
        client, _db, token = api
        client.post("/api/v1/sync/live-push", json={
            "gameweek": 2, "elements": [{"element_id": 1, "points": 0}]
        }, headers=_bearer(token))
        status = client.get("/api/v1/sync/status").json()
        assert status["latest"]["live"]["gameweek"] == 2
        assert status["token_configured"] is True
