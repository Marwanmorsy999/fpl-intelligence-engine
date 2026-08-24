"""Phase 23 Gate 0 — consistency gate tests (v2.2.1-consistency).

C1 single-source market check : Decisions + Captain Spotlight + Sources all
   produce byte-identical text ("matched 10/10 GW2 fixtures · unmatched:
   LEE, NEW") through fpl_intelligence.prediction.market_check.
C2 grading sweeper            : every recommendation whose gameweek results
   are ingested gets a verdict — XI included; identical XIs -> NEUTRAL with
   a reason; nothing stays pending when results exist. /admin/grade-now is
   the one-shot prod fix for stranded GW1 rows.
C3 calibration arms           : /sync/calibration exposes forecast_arms and
   Sources renders "calibration arms: GW{n} forecasts stored".
C4 captain comparison labels  : every number carries its label
   ("xPTS 6.6", "gap +0.6").
C5 assistant history          : the currently-shown brief's gameweek is
   excluded from the history list.
C6 my-team header             : "Gameweek N" pill plus "squad synced during
   GW{m}" sub-line, backed by GET /api/v1/sync/target-gameweek.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from fpl_intelligence.db.models import Team, TeamExternalId
from fpl_intelligence.prediction.market_check import (
    compute_market_status,
    format_market_detail,
)
from fpl_intelligence.sync.materialized_models import FixturesCacheDB
from fpl_intelligence.sync.models import (
    IngestedGameweekDB,
    PredictionLedgerDB,
    RecommendationDB,
)
from fpl_intelligence.sync.scoring import NEUTRAL
from fpl_intelligence.sync.service import score_pending_recommendations

#: The canonical sentence every surface must render (Gate-0 spec example).
EXPECTED_DETAIL = "matched 10/10 GW2 fixtures · unmatched: LEE, NEW"

#: Official-FPL-style club ids -> (short_name, full_name).
CLUBS: dict[int, tuple[str, str]] = {
    1: ("ARS", "Arsenal"),
    2: ("AVL", "Aston Villa"),
    3: ("BOU", "Bournemouth"),
    4: ("BRE", "Brentford"),
    5: ("BHA", "Brighton"),
    6: ("BUR", "Burnley"),
    7: ("CHE", "Chelsea"),
    8: ("CRY", "Crystal Palace"),
    9: ("EVE", "Everton"),
    10: ("FUL", "Fulham"),
    11: ("LEE", "Leeds United"),
    12: ("LIV", "Liverpool"),
    13: ("MCI", "Manchester City"),
    14: ("MUN", "Manchester United"),
    15: ("NEW", "Newcastle United"),
    16: ("NFO", "Nottingham Forest"),
    17: ("SUN", "Sunderland"),
    18: ("TOT", "Tottenham Hotspur"),
    19: ("WHU", "West Ham United"),
    20: ("WOL", "Wolverhampton Wanderers"),
}


def _gw2_pairings() -> list[tuple[int, int]]:
    """Ten home/away pairings covering all 20 clubs exactly once.

    Leeds (11) and Newcastle (15) both face covered opponents, so every
    fixture still matches through one side while both clubs appear as
    unmatched — the exact Gate-0 spec scenario.
    """
    return [
        (1, 7), (2, 3), (4, 5), (6, 8), (9, 10),
        (13, 14), (16, 17), (18, 19), (11, 20), (15, 12),
    ]


def _covered_names() -> set[str]:
    """Bookmaker coverage missing exactly Leeds and Newcastle."""
    from fpl_intelligence.data_providers.team_aliases import canonical_team_name

    covered = set()
    for short, full in CLUBS.values():
        if short in ("LEE", "NEW"):
            continue
        covered.add(canonical_team_name(full))
    return covered


def _id_to_names() -> dict[int, list[str]]:
    return {tid: [short, full] for tid, (short, full) in CLUBS.items()}


class FakeSnapshot:
    """Stand-in for an OddsSnapshot whose names() is what the audit reads."""

    matches = [object()] * 10

    def matched_event_names(self) -> set[str]:
        return set(_covered_names())


class TestC1SingleSourceMarketCheck:
    def test_shared_module_produces_the_canonical_sentence(self):
        rows = [(2, h, a) for h, a in _gw2_pairings()]
        status = compute_market_status(rows, _id_to_names(), _covered_names())
        assert status["fixtures_matched"] == 10
        assert status["fixtures_total"] == 10
        assert status["unmatched"] == ["LEE", "NEW"]
        assert status["detail"] == EXPECTED_DETAIL

    def test_formatter_is_stable(self):
        assert format_market_detail(
            matched=10, total=10, gameweek=2, unmatched=["LEE", "NEW"]
        ) == EXPECTED_DETAIL

    def test_all_three_surfaces_identical(self, db_session):
        """Decisions/Captain payload == Sources probe == shared module output.

        One synthetic universe feeds every surface: teams registered through
        official_fpl external ids, a fixtures cache whose first unfinished GW
        is 2, and one odds snapshot. Sources runs ``odds_probe_payload``;
        Decisions/Captain run ``_shared_market_payload`` — the exact function
        the proxy level stores into chain notes and chain_meta copies into
        report.meta.chain.market_check.
        """
        from datetime import UTC as _UTC

        db = db_session
        for tid, (short, full) in CLUBS.items():
            team = Team(id=tid, name=full, short_name=short)
            db.add(team)
            db.flush()
            db.add(
                TeamExternalId(
                    team_id=tid,
                    provider="official_fpl",
                    provider_team_id=str(tid),
                )
            )
        db.add(
            FixturesCacheDB(
                source="unit-test",
                payload=[
                    {
                        "event": 2,
                        "team_h": h,
                        "team_a": a,
                        "team_h_difficulty": 3,
                        "team_a_difficulty": 3,
                        "finished": False,
                    }
                    for h, a in _gw2_pairings()
                ],
                fetched_at=datetime.now(_UTC),
            )
        )
        db.commit()

        # --- Sources surface -------------------------------------------------
        from fpl_intelligence.api.routes.data_sources import odds_probe_payload

        sources_block = asyncio.run(odds_probe_payload(db, FakeSnapshot()))
        assert sources_block["detail"] == EXPECTED_DETAIL
        assert sources_block["status"] == "ok"
        assert sources_block["enabled"] is True

        # --- Decisions / Captain Spotlight surface ---------------------------
        from fpl_intelligence.prediction.live_provider import _shared_market_payload

        decisions_block = _shared_market_payload(
            db,
            2,
            [{"home_team_id": h, "away_team_id": a} for h, a in _gw2_pairings()],
            FakeSnapshot(),
        )
        assert decisions_block["enabled"] is True
        assert decisions_block["detail"] == EXPECTED_DETAIL

        # Captain Spotlight renders from the SAME meta object as Decisions;
        # both payloads must agree byte-for-byte on every rendered field.
        assert decisions_block["detail"] == sources_block["detail"]
        assert decisions_block["unmatched"] == ["LEE", "NEW"]
        assert sources_block["unmatched"] == ["LEE", "NEW"]

    def test_dashboard_renders_detail_verbatim(self):
        """The static page routes both spots through ONE renderer."""
        html = _read_static("dashboard.html")
        assert "function marketCheckHTML(chain)" in html
        assert html.count("marketCheckHTML(chain)") >= 2, (
            "Captain Spotlight AND the chain banner must use the shared renderer"
        )
        assert "mc.detail ||" in html


class TestC2GradingSweeper:
    @pytest.fixture()
    def gw1_actuals(self, db_session):
        """Ingested GW1 results for 12 fake elements (5 pts each)."""
        db = db_session
        for eid in range(501, 513):
            db.add(
                IngestedGameweekDB(
                    gameweek=1,
                    element_id=eid,
                    source="unit-test",
                    total_points=5,
                    ingested_at=datetime.now(UTC),
                )
            )
        db.commit()
        return db

    def _add_squad_snapshot(self, db, session_key: str, player_ids: list[int]) -> None:
        from fpl_intelligence.squad.models_db import SquadStateDB

        db.merge(
            SquadStateDB(
                session_id=session_key,
                squad_json={
                    "gameweek": 1,
                    "player_ids": list(player_ids),
                },
                updated_at=datetime.now(UTC),
            )
        )

    def test_xi_row_graded_neutral_when_user_xi_matches(self, gw1_actuals):
        db = gw1_actuals
        rec_xi = list(range(501, 512))
        self._add_squad_snapshot(db, "424242", rec_xi + [512])
        db.add(
            RecommendationDB(
                session_key="424242",
                gameweek=1,
                rec_type="xi",
                subject={"xi": rec_xi},
                detail={},
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
        graded = score_pending_recommendations(db, up_to_gameweek=1)
        assert graded == 1
        row = db.scalar(select(RecommendationDB))
        assert row.scored_at is not None
        assert row.score["verdict"] == NEUTRAL
        assert "matched your fielded XI" in row.score["reason"]
        assert row.score["recommended_points"] == row.score["user_points"]

    def test_nothing_stays_pending_once_results_exist(self, gw1_actuals):
        """Missing actuals/user-XI grade NEUTRAL-with-reason, never pending."""
        db = gw1_actuals
        db.add(
            RecommendationDB(
                session_key="no-snapshot",
                gameweek=1,
                rec_type="xi",
                subject={"xi": list(range(501, 512))},
                detail={},
                created_at=datetime.now(UTC),
            )
        )
        db.add(
            RecommendationDB(
                session_key="cap-missing",
                gameweek=1,
                rec_type="captain",
                subject={"captain_id": 99999},  # no such result ingested
                detail={"alternatives": [88888]},
                created_at=datetime.now(UTC),
            )
        )
        db.add(
            RecommendationDB(
                session_key="chip",
                gameweek=1,
                rec_type="chip",
                subject={"chip": "bboost"},
                detail={},
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
        graded = score_pending_recommendations(db, up_to_gameweek=1)
        assert graded == 3
        for row in db.execute(select(RecommendationDB)).scalars().all():
            assert row.scored_at is not None
            assert row.score["verdict"] == NEUTRAL
            assert row.score.get("reason")

    def test_future_gameweeks_without_results_stay_pending(self, gw1_actuals):
        db = gw1_actuals
        db.add(
            RecommendationDB(
                session_key="later",
                gameweek=3,
                rec_type="captain",
                subject={"captain_id": 501},
                detail={"alternatives": [502]},
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
        graded = score_pending_recommendations(db, up_to_gameweek=1)
        assert graded == 0
        row = db.scalar(select(RecommendationDB))
        assert row.scored_at is None

    def test_grade_now_endpoint_sweeps_everything(self, gw1_actuals):
        db = gw1_actuals
        db.add(
            RecommendationDB(
                session_key="oneshot",
                gameweek=1,
                rec_type="chip",
                subject={"chip": "wildcard"},
                detail={},
                created_at=datetime.now(UTC),
            )
        )
        db.commit()

        from fastapi.testclient import TestClient

        from fpl_intelligence.api.main import app
        from fpl_intelligence.db.session import get_db

        app.dependency_overrides[get_db] = lambda: db
        try:
            client = TestClient(app)
            resp = client.get("/api/v1/admin/grade-now")
            body = resp.json()
            assert resp.status_code == 200, body
            assert body["ok"] is True
            assert body["graded_now"] >= 1
            assert body["up_to_gameweek"] == 1
            assert body["still_pending_within_up_to"] == 0
            assert "nothing stays pending" in body["note"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_daily_job_passes_finished_gw_into_sweeper(self):
        src = _read_repo_file("src/fpl_intelligence/api/routes/admin.py")
        assert "score_pending_recommendations(db, up_to_gameweek=fin_gw)" in src


class TestC3CalibrationArms:
    def test_forecast_arms_exposed_and_hint_rendered(self, db_session):
        from fastapi.testclient import TestClient

        from fpl_intelligence.api.main import app
        from fpl_intelligence.db.session import get_db

        db = db_session
        db.add(
            PredictionLedgerDB(
                gameweek=2,
                element_id=501,
                predicted=5.5,
                actual=None,
                created_at=datetime.now(UTC),
            )
        )
        db.commit()

        app.dependency_overrides[get_db] = lambda: db
        try:
            client = TestClient(app)
            resp = client.get("/api/v1/sync/calibration").json()
            arms = resp.get("forecast_arms") or []
            assert any(a["gameweek"] == 2 and a["rows"] == 1 for a in arms)

            html = _read_static("sources.html")
            assert "calibration arms:" in html
            assert "grades automatically once results are ingested" in html
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_daily_job_captures_target_gw_forecasts(self):
        src = _read_repo_file("src/fpl_intelligence/api/routes/admin.py")
        assert "capture_pre_ingest_predictions" in src
        assert "forecast_capture" in src


class TestC4CaptainCardLabels:
    def test_blank_note_labels_every_number(self):
        from fpl_intelligence.squad.depth import captain_comparison

        xi = [
            {"player_id": 1, "web_name": "Haaland", "xpts": 6.6},
            {"player_id": 2, "web_name": "Saka", "xpts": 5.9},
            {"player_id": 3, "web_name": "Saliba", "xpts": 4.8},
        ]
        out = captain_comparison(xi, captain_id=1, vice_id=None)
        card = out["cards"][0]
        assert card["xpts_label"] == "xPTS 6.6"
        assert card["gap_label"] == "gap +0.7"
        assert "(xPTS 5.9)" in card["blank_note"]
        assert "gap +0.7" in card["blank_note"]
        vice_out = captain_comparison(
            [
                {"player_id": 1, "web_name": "Haaland", "xpts": 6.6},
                {"player_id": 4, "web_name": "Palmer", "xpts": 6.1},
            ],
            captain_id=1,
            vice_id=4,
        )
        assert "xPTS 6.1" in vice_out["vice"]["line"]

    def test_dashboard_uses_backend_labels(self):
        html = _read_static("dashboard.html")
        assert "c.xpts_label ||" in html
        assert "data-xpts-label" in html


class TestC5AssistantHistoryExcludesCurrent:
    def test_history_filters_currently_shown_brief(self):
        html = _read_static("assistant.html")
        assert "var CURRENT_GW = null;" in html
        assert "!== Number(CURRENT_GW)" in html
        assert "CURRENT_GW = Number(brief.gameweek);" in html


class TestC6MyTeamHeader:
    def test_header_pill_and_sub_line_present(self):
        html = _read_static("my_team.html")
        assert 'id="gwHeader"' in html
        assert "Gameweek –" in html
        assert 'id="syncSubLine"' in html
        assert '"squad synced during GW" + push.gameweek' in html
        assert '"squad synced during GW" + squadGameweek' in html

    def test_target_gameweek_endpoint(self, db_session, monkeypatch):
        from fastapi.testclient import TestClient

        import fpl_intelligence.sync.gameweek_clock as clock
        from fpl_intelligence.api.main import app
        from fpl_intelligence.db.session import get_db

        async def fake_resolve(db, fallback: int = 1) -> int:
            return 2

        monkeypatch.setattr(clock, "resolve_target_gameweek", fake_resolve)

        app.dependency_overrides[get_db] = lambda: db_session
        try:
            client = TestClient(app)
            body = client.get("/api/v1/sync/target-gameweek").json()
            assert body == {"gameweek": 2}
        finally:
            app.dependency_overrides.pop(get_db, None)


def _read_static(name: str) -> str:
    from pathlib import Path

    path = Path(__file__).parents[2] / "src/fpl_intelligence/web/static" / name
    return path.read_text(encoding="utf-8")


def _read_repo_file(relpath: str) -> str:
    from pathlib import Path

    return (Path(__file__).parents[2] / relpath).read_text(encoding="utf-8")
