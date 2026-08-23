"""Phase 20.1 — production incident: fixtures correctness + materialization.

Two failure classes are pinned here:

1. FIXTURES CORRECTNESS — the old code rendered opponents through a hardcoded
   2025/26 ``TEAM_SHORT_NAMES`` map while the official 2026/27 bootstrap
   reshuffled team ids from #6 up (6=CHE, 7=COV, 11=HUL, 12=IPS, 20=SUN).
   The scanner now accepts a DB-backed names map and these tests assert the
   scanner output equals the OFFICIAL bootstrap fixtures for three real teams.

2. MATERIALIZATION — the daily cron writes ``fixtures_cache`` /
   ``news_cache`` / ``element_facts`` / ``predictions_current``; the request
   paths read only those tables. Parsers, read helpers and the provider fast
   path are covered with captured vaastav CSV payloads.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_intelligence.fixtures.scanner import (
    easiest_team_runs,
    infer_current_gameweek,
    next_gameweeks,
    parse_fixtures,
    player_run,
    team_short_name,
)
from fpl_intelligence.materialize.vaastav import (
    parse_fixtures_csv,
    parse_gw_results_csv,
    parse_players_raw_csv,
)

_OFFICIAL_SLICE = Path(__file__).parent / "_official_fixtures_gw1_5.json"

#: Official 2026/27 bootstrap teams table (captured 2026-08-23).
OFFICIAL_TEAMS_2026_27 = {
    1: "ARS", 2: "AVL", 3: "BOU", 4: "BRE", 5: "BHA",
    6: "CHE", 7: "COV", 8: "CRY", 9: "EVE", 10: "FUL",
    11: "HUL", 12: "IPS", 13: "LEE", 14: "LIV", 15: "MCI",
    16: "MUN", 17: "NEW", 18: "NFO", 19: "TOT", 20: "SUN",
}

#: Ground truth fetched from the official /api/fixtures/ on 2026-08-23:
#: (team_id, gw) -> (opponent_id, is_home). Verified against the live API.
OFFICIAL_RUNS = {
    (13, 1): (18, False),  # Leeds away at Forest
    (13, 2): (4, True),
    (13, 3): (5, False),
    (13, 4): (17, True),
    (13, 5): (8, True),
    (14, 1): (17, False),  # Liverpool at Newcastle
    (14, 2): (18, True),
    (14, 3): (12, False),  # Liverpool at Ipswich
    (14, 4): (10, True),
    (14, 5): (3, False),
    (20, 1): (12, False),  # Sunderland at Ipswich
    (20, 2): (10, True),
    (20, 3): (4, False),
    (20, 4): (1, True),    # Sunderland host Arsenal
    (20, 5): (15, False),
}


def _official_rows() -> list[dict]:
    return json.loads(_OFFICIAL_SLICE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. Fixture correctness vs the official bootstrap payload
# --------------------------------------------------------------------------- #
class TestFixturesMatchOfficial:
    def setup_method(self) -> None:
        self.rows = parse_fixtures(_official_rows())
        self.by_gw: dict[int, list] = {}
        for row in self.rows:
            self.by_gw.setdefault(row.event, []).append(row)
        self.names = dict(OFFICIAL_TEAMS_2026_27)

    def test_current_gameweek_is_first_unfinished(self):
        # Captured slice: GW1 in progress (some started, none finished).
        assert infer_current_gameweek(self.rows) == 1

    @pytest.mark.parametrize("team_id", [13, 14, 20])
    def test_next_five_opponents_and_gws_equal_official(self, team_id: int):
        horizon = next_gameweeks(self.rows, 1, 5)
        runs = player_run(team_id, self.by_gw, horizon, team_names=self.names)
        for run in runs:
            expected = OFFICIAL_RUNS.get((team_id, run.gw))
            assert expected is not None, f"GW{run.gw} missing from ground truth"
            opp_id, is_home = expected
            assert (run.opponent_id, run.is_home) == (opp_id, is_home), (
                f"team {team_id} GW{run.gw}: got opponent {run.opponent_id} "
                f"(home={run.is_home}), official says {opp_id} (home={is_home})"
            )

    @pytest.mark.parametrize(
        ("team_id", "gw", "expected_short"),
        [
            (13, 1, "NFO"),   # was rendered as MCI by the stale map
            (14, 3, "IPS"),   # was rendered as LIV
            (20, 1, "IPS"),
            (20, 4, "ARS"),
            (14, 1, "NEW"),
        ],
    )
    def test_rendered_short_names_match_official_bootstrap(
        self, team_id: int, gw: int, expected_short: str
    ):
        horizon = next_gameweeks(self.rows, 1, 5)
        runs = player_run(team_id, self.by_gw, horizon, team_names=self.names)
        run = next(r for r in runs if r.gw == gw)
        assert run.opponent == expected_short

    def test_stale_static_map_must_not_override_db_map(self):
        # id 7 is Coventry (COV) in 2026/27 but CHE in the stale static map.
        assert OFFICIAL_TEAMS_2026_27[7] == "COV"
        assert team_short_name(7, self.names) == "COV"
        # Without a db map the static fallback still answers something.
        assert team_short_name(7) in {"CHE", "T7"} or team_short_name(7) == "CHE"

    def test_easiest_runs_use_db_names(self):
        horizon = next_gameweeks(self.rows, 1, 4)
        targets = easiest_team_runs(
            self.by_gw, horizon, top=5, exclude_teams=(), team_names=self.names
        )
        assert targets
        for target in targets:
            assert target.short_name == OFFICIAL_TEAMS_2026_27[target.team_id]


# --------------------------------------------------------------------------- #
# 2. vaastav parsers (captured-format payloads)
# --------------------------------------------------------------------------- #
_GW_CSV = """element,total_points,was_home,opponent_team,minutes,bonus,goals_scored,assists,xP
101,8,True,18,90,3,2,0,0.71
102,2,False,17,90,0,0,0,0.12
103,0,True,12,0,0,0,0,0.05
"""

_FIXTURES_CSV = (
    "code,event,finished,finished_provisional,id,kickoff_time,minutes,"
    "provisional_start_time,started,team_a,team_a_score,team_h,team_h_score,"
    "stats,team_h_difficulty,team_a_difficulty,pulse_id\n"
    "2645195,1,False,False,1,2026-08-21T19:00:00Z,0,False,False,7,,1,,[],2,5,0\n"
    "2645198,1,False,False,4,2026-08-22T11:30:00Z,0,False,False,16,,11,,[],4,2,0\n"
)

_PLAYERS_CSV = (
    "id,web_name,team,minutes,selected_by_percent,cost_change_event,status,"
    "news,now_cost\n"
    '411,"Haaland",15,90,"25.3",0,a,"Knock - 75% chance of playing",155\n'
    '426,"B.Fernandes",16,90,"18.1",-1,a,,120\n'
)


class TestVaastavParsers:
    def test_parse_gw_results(self):
        rows = parse_gw_results_csv(_GW_CSV)
        assert len(rows) == 3
        first = rows[0]
        assert first["element_id"] == 101
        assert first["total_points"] == 8
        assert first["minutes"] == 90
        assert first["bonus"] == 3
        assert first["payload"]["opponent_team"] == "18"

    def test_parse_fixtures_csv_matches_official_shape(self):
        out = parse_fixtures_csv(_FIXTURES_CSV)
        assert out == [
            {
                "event": 1,
                "team_h": 1,
                "team_a": 7,
                "team_h_difficulty": 2,
                "team_a_difficulty": 5,
                "finished": False,
                "kickoff_time": "2026-08-21T19:00:00Z",
            },
            {
                "event": 1,
                "team_h": 11,
                "team_a": 16,
                "team_h_difficulty": 4,
                "team_a_difficulty": 2,
                "finished": False,
                "kickoff_time": "2026-08-22T11:30:00Z",
            },
        ]
        # And the scanner consumes it identically to the live-FPL payload.
        parsed = parse_fixtures(out)
        assert len(parsed) == 2

    def test_parse_players_raw_facts(self):
        facts = parse_players_raw_csv(_PLAYERS_CSV)
        assert facts[411]["web_name"] == "Haaland"
        assert facts[411]["team_id"] == 15
        assert facts[411]["status"] == "a"
        assert facts[411]["news"].startswith("Knock")
        assert facts[426]["cost_change_event"] == -1
        assert facts[426]["news"] is None


# --------------------------------------------------------------------------- #
# 3. Materialized read helpers + provider fast path
# --------------------------------------------------------------------------- #
def _seed_team(db, *, provider_id: str, short: str) -> None:
    from fpl_intelligence.db.models import Team, TeamExternalId

    team = Team(name=f"Team {short}", short_name=short)
    db.add(team)
    db.flush()
    db.add(TeamExternalId(team_id=team.id, provider="official_fpl", provider_team_id=provider_id))
    db.flush()


class TestReadHelpers:
    def test_load_cached_fixtures_roundtrip(self, db_session):
        from fpl_intelligence.materialize import load_cached_fixtures
        from fpl_intelligence.sync.materialized_models import FixturesCacheDB

        assert load_cached_fixtures(db_session) == []
        db_session.add(
            FixturesCacheDB(
                source="vaastav:2026-27",
                payload=_official_rows()[:5],
                fetched_at=datetime.now(UTC),
            )
        )
        db_session.commit()
        cached = load_cached_fixtures(db_session)
        assert len(cached) == 5
        assert cached[0]["team_h"] == 1

    def test_load_cached_fixtures_rejects_stale(self, db_session):
        from fpl_intelligence.materialize import load_cached_fixtures
        from fpl_intelligence.sync.materialized_models import FixturesCacheDB

        db_session.add(
            FixturesCacheDB(
                source="vaastav:2026-27",
                payload=_official_rows()[:2],
                fetched_at=datetime.now(UTC) - timedelta(days=10),
            )
        )
        db_session.commit()
        assert load_cached_fixtures(db_session) == []

    def test_cached_news_items_roundtrip_and_staleness(self, db_session):
        from datetime import datetime as dt

        from fpl_intelligence.api.routes.news import cached_items_from_db
        from fpl_intelligence.sync.materialized_models import NewsCacheDB

        db_session.add(
            NewsCacheDB(
                source="bbc-rss",
                headline_count=2,
                payload=[
                    {"title": "Star striker injured", "link": "x", "published": "now"},
                    {"title": "Transfer done", "link": "y", "published": "then"},
                ],
                fetched_at=dt.now(UTC),
            )
        )
        db_session.commit()
        items, fetched_at = cached_items_from_db(db_session)
        assert [i.title for i in items] == ["Star striker injured", "Transfer done"]
        assert fetched_at is not None

    def test_team_names_from_db_joins_external_ids(self, db_session):
        from fpl_intelligence.materialize import team_names_from_db

        for pid, short in ((7, "COV"), (13, "LEE")):
            _seed_team(db_session, provider_id=str(pid), short=short)
        names = team_names_from_db(db_session)
        assert names == {7: "COV", 13: "LEE"}


class TestMaterializedFastPath:
    def _provider(self, db):
        from fpl_intelligence.prediction.live_provider import LivePredictionProvider

        return LivePredictionProvider(session=db)

    def _seed_predictions(self, db, gameweek: int, *, computed_at=None, n=60):
        from fpl_intelligence.sync.materialized_models import PredictionCurrentDB

        when = computed_at or datetime.now(UTC)
        for pid in range(1, n + 1):
            db.add(
                PredictionCurrentDB(
                    gameweek=gameweek,
                    element_id=pid,
                    expected_points=1.0 + pid * 0.01,
                    minutes_estimate=70.0,
                    start_prob=0.8,
                    source="pre-season-proxy-v2",
                    data_quality="heuristic-proxy-enriched",
                    computed_at=when,
                )
            )
        db.commit()

    def test_resolve_chain_serves_materialized_table(self, db_session):
        provider = self._provider(db_session)
        self._seed_predictions(db_session, 2)

        result = provider.resolve_chain(2)
        from fpl_intelligence.prediction.live_provider import SOURCE_MATERIALIZED

        assert result.source == SOURCE_MATERIALIZED
        assert result.resolved.covered == 60

        preds = provider.get_squad_predictions([5], [2])
        assert preds[2][5].expected_points == pytest.approx(1.05, abs=1e-6)
        assert preds[2][5].source == SOURCE_MATERIALIZED

    def test_stale_table_falls_back_to_inline_chain(self, db_session):
        provider = self._provider(db_session)
        self._seed_predictions(
            db_session, 3, computed_at=datetime.now(UTC) - timedelta(days=3)
        )
        # Freshness gate rejects the stale rows; the inline chain takes over
        # and its provenance must never claim the materialized source.
        from fpl_intelligence.prediction.live_provider import (
            SOURCE_MATERIALIZED,
            SOURCE_PROXY,
        )

        result = provider.resolve_chain(3)
        assert result.source != SOURCE_MATERIALIZED
        assert result.source in {SOURCE_PROXY} or result.levels

    def test_thin_coverage_falls_back(self, db_session):
        provider = self._provider(db_session)
        self._seed_predictions(db_session, 4, n=10)  # below the 50-player floor
        from fpl_intelligence.prediction.live_provider import (
            SOURCE_MATERIALIZED,
            SOURCE_PROXY,
        )

        result = provider.resolve_chain(4)
        assert result.source != SOURCE_MATERIALIZED
        assert result.source in {SOURCE_PROXY} or result.levels
