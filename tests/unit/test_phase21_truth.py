"""Phase 21.1 — TRUTH gate: results ingestion, gameweek clock, cached briefs,
odds alias mapping and the Understat honest-label fix."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fpl_intelligence.data_providers.fpl_egress import FplEgressExhaustedError
from fpl_intelligence.db.session import get_db
from fpl_intelligence.fixtures.scanner import next_unplayed_gameweeks, parse_fixtures
from fpl_intelligence.sync.gameweek_clock import pick_target_event
from fpl_intelligence.sync.results_ingestion import (
    finished_gameweeks_from_fixtures,
    parse_event_live,
    validate_event_live_payload,
)
from fpl_intelligence.sync.scoring import score_captain

# --------------------------------------------------------------------------- #
# T1 — event/{gw}/live parsing + finished-GW detection
# --------------------------------------------------------------------------- #


class TestEventLiveParsing:
    def test_validator_accepts_elements_list(self):
        assert validate_event_live_payload({"elements": [], "fixtures": []})
        assert not validate_event_live_payload({"nope": []})
        assert not validate_event_live_payload(None)

    def test_parse_extracts_points_minutes_goals_assists_bonus(self):
        payload = {
            "elements": [
                {"id": 411, "stats": {"total_points": 13, "minutes": 90, "bonus": 3,
                                      "goals_scored": 2, "assists": 0,
                                      "expected_goal_involvements": "1.80"}},
                {"id": 12, "stats": {"total_points": 2, "minutes": 65}},
                {"id": "bad"},
            ]
        }
        rows = parse_event_live(payload)
        assert [r["element_id"] for r in rows] == [411, 12]
        haaland = rows[0]
        assert haaland["total_points"] == 13
        assert haaland["minutes"] == 90
        assert haaland["bonus"] == 3
        assert haaland["goals_scored"] == 2
        assert haaland["assists"] == 0
        assert haaland["xgi"] == pytest.approx(1.8)
        assert "bonus" not in rows[1]

    def test_finished_gameweeks_requires_all_fixtures_finished(self):
        fixtures = [
            {"event": 1, "finished": True},
            {"event": 1, "finished": True},
            {"event": 2, "finished": True},
            {"event": 2, "finished": False},
        ]
        assert finished_gameweeks_from_fixtures(fixtures) == [1]
        assert finished_gameweeks_from_fixtures([]) == []


class TestResultsIngestionFlow:
    @pytest.fixture()
    def api(self, db_session):
        from fpl_intelligence.api.main import app

        app.dependency_overrides[get_db] = lambda: db_session
        client = TestClient(app)
        yield client, db_session
        app.dependency_overrides.pop(get_db, None)

    def _seed_fixtures_cache(self, db, *, gw1_finished=True):
        from fpl_intelligence.sync.materialized_models import FixturesCacheDB

        payload = [
            {"event": 1, "team_h": 1, "team_a": 2, "finished": gw1_finished},
            {"event": 1, "team_h": 3, "team_a": 4, "finished": gw1_finished},
            {"event": 2, "team_h": 1, "team_a": 3, "finished": False},
            {"event": 2, "team_h": 2, "team_a": 4, "finished": False},
        ]
        db.add(FixturesCacheDB(source="test", payload=payload, fetched_at=datetime.now(UTC)))
        db.commit()

    def test_ingest_flips_pending_recommendation_to_graded_with_names(
        self, api, monkeypatch
    ):
        client, db = api
        self._seed_fixtures_cache(db)

        # A pending captain recommendation for GW1: picked Haaland (id 411).
        from fpl_intelligence.squad.models_db import SquadStateDB
        from fpl_intelligence.sync.service import record_recommendations

        db.add(SquadStateDB(
            session_id="794561",
            squad_json={
                "player_ids": [411, 12],
                "captain_id": 411,
                "vice_captain_id": 12,
                "bank": 1.0,
                "free_transfers": 1,
                "chips_available": [],
                "gameweek": 2,
                "player_positions": {},
                "player_prices": {},
                "player_teams": {},
            },
            updated_at=datetime.now(UTC),
        ))

        class _Cap:
            player_id = 411
            expected_points = 6.5
            expected_gain = 6.5
            probability_positive = 0.5
            confidence = 0.5
            main_reason = "easy fixture"
            main_risk = ""

        class _Report:
            gameweek = 1
            starting_xi = [411, 12]
            bench_order = []
            captain = _Cap()
            vice_captain = 12
            transfer_plan = None
            chip_recommendation = None

        record_recommendations(db, session_key="794561", report=_Report())
        db.commit()

        # The official live endpoint returns Haaland 26 (2 goals doubled math
        # aside — raw points), Saka 2.
        async def fake_fetch(gw, settings=None):
            return (
                parse_event_live({
                    "elements": [
                        {"id": 411, "stats": {"total_points": 13, "minutes": 90}},
                        {"id": 12, "stats": {"total_points": 2, "minutes": 90}},
                    ]
                }),
                "allorigins",
            )

        monkeypatch.setattr(
            "fpl_intelligence.sync.results_ingestion.fetch_event_live", fake_fetch
        )

        resp = client.post("/api/v1/admin/ingest-results?force=1")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ingested = body["report"]["ingested"]
        assert ingested and ingested[0]["gameweek"] == 1
        assert ingested[0]["via"] == "allorigins"
        assert ingested[0]["recommendations_scored"] >= 1

        # Track record now shows a graded card with a resolved name.
        tr = client.get("/api/v1/sync/track-record?entry_id=794561").json()
        card = next(c for c in tr["cards"] if c["rec_type"] == "captain")
        assert card["scored"] is True
        assert card["subject"]["captain_name"] == "Haaland"
        assert card["score"]["captain_name"] == "Haaland"
        alt = card["score"]
        assert alt["alternative_name"] == "Saka" or alt["best_alternative"] == 12
        assert tr["rolling"]["graded"] == 1
        assert tr["rolling"]["hit_rate"] is not None

        # Sources-style read: history exists.
        from sqlalchemy import select

        from fpl_intelligence.sync.models import IngestedGameweekDB

        rows = db.execute(select(IngestedGameweekDB)).scalars().all()
        assert {(r.gameweek, r.element_id) for r in rows} == {(1, 411), (1, 12)}
        assert all(r.source == "fpl-live" for r in rows)

    def test_fetch_failure_is_reported_not_raised(self, api, monkeypatch):
        client, db = api
        self._seed_fixtures_cache(db)

        async def fail_fetch(gw, settings=None):
            return None, FplEgressExhaustedError("/api/event/1/live/", [])

        monkeypatch.setattr(
            "fpl_intelligence.sync.results_ingestion.fetch_event_live", fail_fetch
        )
        resp = client.post("/api/v1/admin/ingest-results")
        assert resp.status_code == 200
        skipped = resp.json()["report"]["skipped"]
        assert any("fetch failed" in s["reason"] for s in skipped)


# --------------------------------------------------------------------------- #
# T2 — gameweek auto-advance
# --------------------------------------------------------------------------- #


class TestTargetGameweek:
    def test_next_deadline_event_wins(self):
        events = [
            {"id": 1, "deadline_time": "2026-08-21T18:00:00Z"},
            {"id": 2, "deadline_time": "2026-08-28T18:00:00Z"},
            {"id": 3, "deadline_time": "2026-09-04T18:00:00Z"},
        ]
        now = datetime(2026, 8, 23, tzinfo=UTC)
        assert pick_target_event(events, now) == 2

    def test_after_deadline_passes_target_advances(self):
        events = [
            {"id": 1, "deadline_time": "2026-08-21T18:00:00Z"},
            {"id": 2, "deadline_time": "2026-08-28T18:00:00Z"},
        ]
        now = datetime(2026, 8, 30, tzinfo=UTC)
        assert pick_target_event(events, now) is None  # caller falls back

    def test_unparseable_deadlines_are_skipped(self):
        events = [{"id": 1}, {"id": 2, "deadline_time": "not-a-date"}]
        assert pick_target_event(events, datetime.now(UTC)) is None


class TestUnplayedHorizon:
    ROWS_RAW = [
        {"event": 1, "team_h": 1, "team_a": 2, "finished": True},
        {"event": 2, "team_h": 1, "team_a": 3, "finished": True},
        {"event": 2, "team_h": 2, "team_a": 4, "finished": False},  # GW2 half-played
        {"event": 3, "team_h": 1, "team_a": 4, "finished": False},
        {"event": 4, "team_h": 2, "team_a": 3, "finished": False},
        {"event": 5, "team_h": 1, "team_a": 2, "finished": False},
        {"event": 6, "team_h": 3, "team_a": 4, "finished": False},
        {"event": 7, "team_h": 1, "team_a": 3, "finished": False},
    ]

    def test_horizon_keeps_partially_played_current_and_drops_finished(self):
        rows = parse_fixtures(self.ROWS_RAW)
        horizon = next_unplayed_gameweeks(rows, current_gw=2, count=5)
        assert horizon == [2, 3, 4, 5, 6]


# --------------------------------------------------------------------------- #
# T1 — track-record names + scoring math sanity at the service boundary
# --------------------------------------------------------------------------- #


class TestScoringWithNames:
    def test_captain_delta_is_doubled(self):
        result = score_captain(411, [12], {411: 13, 12: 2})
        assert result["delta"] == (13 - 2) * 2
        assert result["verdict"] == "right"


# --------------------------------------------------------------------------- #
# T3 — analyst/brief are cache-readers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_brief_cache():
    """Module-level brief caches must not leak between tests."""
    from fpl_intelligence.api.routes import assistant as assistant_mod

    before = dict(assistant_mod._brief_cache)
    assistant_mod._brief_cache.clear()
    yield
    assistant_mod._brief_cache.clear()
    assistant_mod._brief_cache.update(before)


@pytest.fixture()
def brief_api(db_session):
    from fpl_intelligence.api.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    yield client, db_session
    app.dependency_overrides.pop(get_db, None)


def _save_squad(db, session_id="794561"):
    from fpl_intelligence.squad.models_db import SquadStateDB

    ids = list(range(101, 116))
    db.add(SquadStateDB(
        session_id=session_id,
        squad_json={
            "player_ids": ids,
            "captain_id": 101,
            "vice_captain_id": 102,
            "bank": 1.0,
            "free_transfers": 1,
            "chips_available": [],
            "gameweek": 2,
            "player_positions": {},
            "player_prices": {},
            "player_teams": {},
        },
        updated_at=datetime.now(UTC),
    ))
    db.commit()


def _store_brief_row(db, session_id="794561", gw=2):
    from fpl_intelligence.sync.materialized_models import AssistantBriefDB

    db.add(AssistantBriefDB(
        session_id=session_id,
        gameweek=gw,
        model="groq/test-model",
        payload={
            "session_id": session_id,
            "gameweek": gw,
            "model": "groq/test-model",
            "sections": {"CAPTAIN": "Haaland is the armband pick."},
            "tldr": [{"kind": "CAPTAIN", "text": "CAPTAIN: Haaland", "confidence": 70}],
            "generated_at": datetime.now(UTC).isoformat(),
        },
        generated_at=datetime.now(UTC),
    ))
    db.commit()


class TestCachedBriefReads:
    def test_brief_serves_persisted_row_without_llm(self, brief_api, monkeypatch):
        client, db = brief_api
        _save_squad(db)
        _store_brief_row(db)

        def explode(*_a, **_k):  # any LLM construction attempt fails the test
            raise AssertionError("LLM provider must never be built on-request")

        monkeypatch.setattr(
            "fpl_intelligence.api.routes.assistant._build_real_provider", explode
        )
        resp = client.get("/api/v1/assistant/brief?session_id=794561")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cached"] is True
        assert body["source"] == "database"
        assert body["model"] == "groq/test-model"

    def test_brief_miss_falls_back_to_template_fast(self, brief_api, monkeypatch):
        client, db = brief_api
        _save_squad(db)

        def explode(*_a, **_k):
            raise AssertionError("template path must stay LLM-free")

        monkeypatch.setattr(
            "fpl_intelligence.api.routes.assistant._build_real_provider", explode
        )
        resp = client.get("/api/v1/assistant/brief?session_id=794561")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["model"] == "template-fallback"
        assert body["cached"] is False
        assert set(body["sections"]) == {
            "SQUAD STATUS", "CAPTAIN", "TRANSFERS",
            "FIXTURE SWINGS", "NEWS FLAGS", "LAST WEEK GRADE",
        }

    def test_analyst_summary_reads_pre_generated_brief(self, brief_api, monkeypatch):
        client, db = brief_api
        _save_squad(db)
        _store_brief_row(db)
        resp = client.get("/api/v1/analyst/summary?session_id=794561")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cached"] is True
        assert "Haaland" in body["summary"]
        assert body["model"].startswith("pre-generated")


# --------------------------------------------------------------------------- #
# T4 — odds alias normalisation
# --------------------------------------------------------------------------- #


class TestOddsAliasMapping:
    def _snapshot(self):
        from fpl_intelligence.data_providers.odds_api import MatchOdds, OddsSnapshot

        match = MatchOdds(
            event_id="1",
            commence_time="2026-08-29T14:00:00Z",
            home_team="Manchester City",
            away_team="Nottingham Forest",
            home_win_prob=0.7,
            away_win_prob=0.3,
        )
        return OddsSnapshot(matches=[match])

    def test_abbreviation_resolves_to_full_name(self):
        snap = self._snapshot()
        hit = snap.for_team("Man City")
        assert hit is not None
        assert hit.prob_for_team("MCI") == pytest.approx(0.7)
        assert hit.prob_for_team("Nott'm Forest") == pytest.approx(0.3)

    def test_unknown_name_returns_none(self):
        assert self._snapshot().for_team("Real Madrid") is None

    def test_matched_event_names_cover_variants(self):
        covered = {name.lower() for name in self._snapshot().matched_event_names()}
        assert "manchester city" in covered
        assert "nottingham forest" in covered


# --------------------------------------------------------------------------- #
# T5 — Understat snapshot age reads meta.fetched_at (the 2864.8d bug)
# --------------------------------------------------------------------------- #


class TestUnderstatSnapshotAge:
    def test_age_comes_from_meta_not_file_mtime(self, tmp_path: Path):
        from fpl_intelligence.api.routes.data_sources import _snapshot_age_and_seasons

        recent = datetime.now(UTC) - timedelta(hours=36)
        path = tmp_path / "understat_snapshot.json"
        path.write_text(json.dumps({
            "meta": {
                "fetched_at": recent.isoformat(),
                "seasons": ["2025", "2026"],
            },
            "seasons": {},
        }), encoding="utf-8")

        # Make the FILE look ancient — mtime lies on deployed bundles.
        ancient = (datetime.now(UTC) - timedelta(days=2900)).timestamp()
        import os

        os.utime(path, (ancient, ancient))

        age_days, seasons = _snapshot_age_and_seasons(str(path))
        assert age_days < 3.0
        assert seasons == ["2025", "2026"]

    def test_missing_meta_falls_back_to_mtime(self, tmp_path: Path):
        from fpl_intelligence.api.routes.data_sources import _snapshot_age_and_seasons

        path = tmp_path / "snap.json"
        path.write_text("{}", encoding="utf-8")
        age_days, seasons = _snapshot_age_and_seasons(str(path))
        assert age_days is not None and age_days < 1.0
        assert seasons == []


# --------------------------------------------------------------------------- #
# T5/D5 — news matching folds accents and initials
# --------------------------------------------------------------------------- #


class TestNewsFoldMatching:
    ITEMS = [
        ("Bruno Fernandes injury doubt for United", "2026-08-22T10:00:00+00:00"),
        ("B.Fernandes returns to training", "2026-08-23T09:00:00+00:00"),
        ("Raya Martín kept another clean sheet", "2026-08-23T12:00:00+00:00"),
    ]

    def _items(self):
        from fpl_intelligence.data_providers.bbc_news import NewsItem

        return [
            NewsItem(title=title, link="https://bbc.example", published=published)
            for title, published in self.ITEMS
        ]

    def test_initial_dot_surname_alias_matches(self):
        from fpl_intelligence.data_providers.bbc_news import NEWS_KEYWORDS, match_headlines

        flags = match_headlines(
            self._items(),
            [(17, "B.Fernandes", "Bruno", "Fernandes")],
            NEWS_KEYWORDS,
        )
        assert "17" in flags
        # newest matching headline wins by parsed timestamp, not string order
        assert "returns to training" in flags["17"]["headline"]

    def test_accent_fold_matches_martin(self):
        from fpl_intelligence.data_providers.bbc_news import build_aliases

        aliases = build_aliases("Raya", "David", "Raya Martín")
        assert any(alias == "raya martin" for alias in aliases)

    def test_plain_surname_token_matches(self):
        from fpl_intelligence.data_providers.bbc_news import NEWS_KEYWORDS, match_headlines

        flags = match_headlines(
            self._items(),
            [(17, "B.Fernandes", "Bruno", "Fernandes")],
            NEWS_KEYWORDS,
        )
        assert "17" in flags  # surname-token alias matched both headlines
