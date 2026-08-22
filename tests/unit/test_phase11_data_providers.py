"""Phase 11.1 unit tests — API-first structured data integration.

Every connector is exercised with a mocked ``httpx.MockTransport`` (no live
network call), every parsing path is fed fixture JSON, the cache hit/miss/TTL
behaviour is asserted with an injected clock, and the injector / decision bridge
are tested for both override application and graceful degradation.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from fpl_intelligence.data_providers import (
    ApiFootballConnector,
    DataConnectionError,
    DataParseError,
    DataProviderDisabledError,
    FactCollectionService,
    FactOverride,
    FactOverrideProvider,
    FactSource,
    FootballDataOrgConnector,
    FplOfficialConnector,
    LiveFactInjector,
    LiveFactResult,
    PlayerFact,
    ResponseCache,
    parse_competitions,
    parse_injuries,
    parse_lineups,
    parse_matches,
    parse_standings,
)
from fpl_intelligence.live_intelligence.bridge import StaticPredictionProvider
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts.fetch_live_facts import parse_args, run, summarize  # noqa: E402

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Build an httpx.Client whose transport is entirely mocked (no network)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _noop_sleep(_seconds: float) -> None:
    return None


def _fpl_bootstrap_payload() -> dict:
    return {
        "elements": [
            {
                "id": 411,
                "web_name": "Salah",
                "first_name": "Mohamed",
                "second_name": "Salah",
                "news": "Salah is suspended for this fixture.",
                "status": "s",
                "chance_of_playing_next_round": 0,
                "chance_of_playing_this_round": 0,
                "now_cost": 130,
                "team": 14,
                "expected_minutes": 0,
            },
            {
                "id": 615,
                "web_name": "Palmer",
                "first_name": "Cole",
                "second_name": "Palmer",
                "news": "",
                "status": "a",
                "chance_of_playing_next_round": 100,
                "chance_of_playing_this_round": 100,
                "now_cost": 110,
                "team": 10,
                "expected_minutes": 90,
            },
            {
                "id": 859,
                "web_name": "Haaland",
                "first_name": "Erling",
                "second_name": "Haaland",
                "news": "",
                "status": "d",
                "chance_of_playing_next_round": 50,
                "chance_of_playing_this_round": 50,
                "now_cost": 140,
                "team": 11,
                "expected_minutes": 45,
            },
        ],
        "teams": [
            {"id": 14, "name": "Liverpool"},
            {"id": 10, "name": "Chelsea"},
            {"id": 11, "name": "Man City"},
        ],
    }


def _fpl_fixtures_payload() -> list[dict]:
    return [
        {
            "id": 1,
            "event": 5,
            "team_h": 14,
            "team_a": 10,
            "team_h_difficulty": 3,
            "team_a_difficulty": 2,
            "finished": False,
        },
        {
            "id": 2,
            "event": 5,
            "team_h": 11,
            "team_a": 14,
            "team_h_difficulty": 2,
            "team_a_difficulty": 4,
            "finished": False,
        },
    ]


def _fpl_routing_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/bootstrap-static/"):
        return httpx.Response(200, json=_fpl_bootstrap_payload())
    if path.endswith("/fixtures/"):
        return httpx.Response(200, json=_fpl_fixtures_payload())
    return httpx.Response(404)


# ---------------------------------------------------------------------------
# ResponseCache
# ---------------------------------------------------------------------------


class TestResponseCache:
    def test_make_key_stable_and_sorted(self):
        a = ResponseCache.make_key("http://x", {"b": 2, "a": 1})
        b = ResponseCache.make_key("http://x", {"a": 1, "b": 2})
        assert a == b
        assert a == "http://x?a=1&b=2"

    def test_make_key_no_params(self):
        assert ResponseCache.make_key("http://x") == "http://x"

    def test_miss_then_store_then_hit(self):
        cache = ResponseCache(clock=lambda: 0.0)
        assert cache.get("ep") is None
        cache.store("ep", value={"x": 1})
        assert cache.get("ep") == {"x": 1}
        assert cache.stats.hits == 1
        assert cache.stats.misses == 1
        assert cache.stats.stores == 1

    def test_general_ttl_expiry(self):
        clock = {"t": 0.0}
        cache = ResponseCache(default_ttl_seconds=10, clock=lambda: clock["t"])
        cache.store("ep", value=1)
        clock["t"] = 9.0
        assert cache.get("ep") == 1
        clock["t"] = 11.0
        assert cache.get("ep") is None
        assert cache.stats.expired == 1

    def test_sensitive_ttl_shorter(self):
        clock = {"t": 0.0}
        cache = ResponseCache(
            default_ttl_seconds=100, sensitive_ttl_seconds=5, clock=lambda: clock["t"]
        )
        cache.store("ep", value=1, sensitive=True)
        clock["t"] = 6.0
        assert cache.get("ep", sensitive=True) is None

    def test_get_or_fetch_uses_cache(self):
        clock = {"t": 0.0}
        cache = ResponseCache(clock=lambda: clock["t"])
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return 42

        assert cache.get_or_fetch("ep", fetch_fn=fetch) == 42
        assert cache.get_or_fetch("ep", fetch_fn=fetch) == 42
        assert calls["n"] == 1

    def test_file_persistence_roundtrip(self, tmp_path: Path):
        d = tmp_path / "cache"
        cache = ResponseCache(cache_dir=d, clock=lambda: 1000.0)
        cache.store("ep", value={"v": 7})
        cache2 = ResponseCache(cache_dir=d, clock=lambda: 1000.0)
        assert cache2.get("ep") == {"v": 7}

    def test_clear_resets(self):
        cache = ResponseCache(clock=lambda: 0.0)
        cache.store("ep", value=1)
        cache.clear()
        assert cache.get("ep") is None
        assert cache.stats.hits == 0

    def test_negative_ttl_rejected(self):
        with pytest.raises(ValueError):
            ResponseCache(default_ttl_seconds=-1)


# ---------------------------------------------------------------------------
# Official FPL connector — parsing
# ---------------------------------------------------------------------------


class TestFplOfficialParsing:
    def test_parse_bootstrap_players(self):
        facts = FplOfficialConnector.parse_bootstrap(_fpl_bootstrap_payload())
        assert len(facts) == 3
        salah = next(f for f in facts if f.fpl_player_id == 411)
        assert salah.name == "Salah"
        assert salah.status == "suspended"
        assert salah.chance_of_playing == 0
        assert salah.price == 13.0
        assert salah.team_name == "Liverpool"
        assert salah.expected_minutes == 0

    def test_parse_bootstrap_price_scaling(self):
        facts = FplOfficialConnector.parse_bootstrap(_fpl_bootstrap_payload())
        palmer = next(f for f in facts if f.fpl_player_id == 615)
        assert palmer.price == 11.0

    def test_parse_bootstrap_status_mapping(self):
        facts = FplOfficialConnector.parse_bootstrap(_fpl_bootstrap_payload())
        haaland = next(f for f in facts if f.fpl_player_id == 859)
        assert haaland.status == "doubtful"
        assert haaland.chance_of_playing == 50

    def test_parse_fixtures_filters_and_difficulty(self):
        out = FplOfficialConnector.parse_fixtures(_fpl_fixtures_payload(), team_id=14)
        assert len(out) == 2
        out.sort(key=lambda f: f["gameweek"])
        assert out[0]["difficulty"] == 3
        assert out[1]["difficulty"] == 4

    def test_next_fixture_difficulty(self):
        conn = FplOfficialConnector()
        assert conn.next_fixture_difficulty(_fpl_fixtures_payload(), 11) == 2

    def test_fetch_player_facts_annotates_difficulty(self):
        conn = FplOfficialConnector(
            http_client=make_client(_fpl_routing_handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        facts = conn.collect_player_facts()
        salah = next(f for f in facts if f.fpl_player_id == 411)
        assert salah.fixture_difficulty == 3

    def test_fetch_element_summary_network(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "element-summary/411" in request.url.path
            return httpx.Response(200, json={"fixtures": [], "history": []})

        conn = FplOfficialConnector(
            http_client=make_client(handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        payload = conn.fetch_element_summary(411)
        assert payload == {"fixtures": [], "history": []}

    def test_fetch_bootstrap_invalid_json_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json")

        conn = FplOfficialConnector(
            http_client=make_client(handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        with pytest.raises(DataParseError):
            conn.fetch_bootstrap()

    def test_fetch_http_500_raises_connection_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        conn = FplOfficialConnector(
            http_client=make_client(handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        with pytest.raises(DataConnectionError):
            conn.collect_player_facts()

    def test_cache_prevents_second_network_call(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=_fpl_bootstrap_payload())

        conn = FplOfficialConnector(
            http_client=make_client(handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        conn.fetch_bootstrap()
        conn.fetch_bootstrap()
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# API-Football connector
# ---------------------------------------------------------------------------


def _lineups_payload() -> dict:
    return {
        "response": [
            {
                "team": {"id": 14, "name": "Liverpool", "logo": "x"},
                "formation": "4-3-3",
                "startXI": [
                    {"player": {"id": 900, "name": "Alisson", "number": 1, "pos": "G"}},
                    {"player": {"id": 901, "name": "Van Dijk", "number": 4, "pos": "D"}},
                ],
                "substitutes": [
                    {"player": {"id": 902, "name": "Bench GK", "number": 13, "pos": "G"}},
                ],
            }
        ]
    }


def _injuries_payload() -> dict:
    return {
        "response": [
            {
                "player": {"id": 903, "name": "Injured Star"},
                "team": {"id": 10, "name": "Chelsea"},
                "fixture": {"id": 1},
                "type": "Injured",
                "reason": {"type": "muscle"},
            },
            {
                "player": {"id": 904, "name": "Fit Player"},
                "team": {"id": 10, "name": "Chelsea"},
                "fixture": {"id": 1},
                "type": "Not injured",
            },
        ]
    }


class TestApiFootballParsing:
    def test_parse_lineups_starting_and_bench(self):
        facts = parse_lineups(_lineups_payload(), fpl_id_map={900: 411, 902: 859})
        starters = [f for f in facts if f.is_starting]
        bench = [f for f in facts if f.is_bench]
        assert len(starters) == 2
        assert len(bench) == 1
        alisson = next(f for f in starters if f.api_football_player_id == 900)
        assert alisson.fpl_player_id == 411
        assert alisson.expected_minutes == 90
        sub = bench[0]
        assert sub.fpl_player_id == 859
        assert sub.expected_minutes == 0

    def test_parse_injuries_filters_non_injured(self):
        facts = parse_injuries(_injuries_payload(), fpl_id_map={903: 615})
        assert len(facts) == 1
        assert facts[0].fpl_player_id == 615
        assert facts[0].is_injured
        assert facts[0].status == "out"

    def test_fetch_lineups_network(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["fixture"] == "55"
            return httpx.Response(200, json=_lineups_payload())

        conn = ApiFootballConnector(
            api_key="k",
            http_client=make_client(handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        facts = conn.fetch_lineups(55)
        assert len(facts) == 3

    def test_fetch_injuries_network(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_injuries_payload())

        conn = ApiFootballConnector(
            api_key="k",
            http_client=make_client(handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        facts = conn.fetch_injuries(date="2026-08-22")
        assert len(facts) == 1

    def test_disabled_when_no_key(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        conn = ApiFootballConnector()
        assert conn.is_enabled() is False

    def test_enabled_when_key_provided(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        conn = ApiFootballConnector(api_key="secret-key")
        assert conn.is_enabled() is True

    def test_disabled_collect_returns_empty_no_network(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        conn = ApiFootballConnector()
        assert conn.collect_player_facts(date="2026-08-22") == []

    def test_require_enabled_raises_when_disabled(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        conn = ApiFootballConnector()
        with pytest.raises(DataProviderDisabledError):
            conn.fetch_lineups(1)

    def test_network_failure_does_not_raise_in_collect(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        conn = ApiFootballConnector(
            api_key="k",
            http_client=make_client(lambda req: (_ for _ in ()).throw(httpx.ConnectError("down"))),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        assert conn.collect_player_facts(date="2026-08-22") == []

    def test_api_error_envelope_raises_connection(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"errors": {"message": "limit"}, "response": []})

        conn = ApiFootballConnector(
            api_key="k",
            http_client=make_client(handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        with pytest.raises(DataConnectionError):
            conn.fetch_lineups(1)


# ---------------------------------------------------------------------------
# football-data.org connector
# ---------------------------------------------------------------------------


class TestFootballDataOrgParsing:
    def test_parse_competitions(self):
        payload = {
            "competitions": [
                {"id": 2021, "name": "Premier League", "code": "PL", "area": {"name": "England"}},
                {"id": 2014, "name": "La Liga", "code": "PD", "area": {"name": "Spain"}},
            ]
        }
        comps = parse_competitions(payload)
        assert len(comps) == 2
        assert comps[0].id == 2021
        assert comps[0].code == "PL"

    def test_parse_matches(self):
        payload = {
            "matches": [
                {
                    "id": 1,
                    "utcDate": "2026-08-22T14:00Z",
                    "status": "SCHEDULED",
                    "homeTeam": {"name": "Arsenal"},
                    "awayTeam": {"name": "Chelsea"},
                    "competition": {"code": "PL"},
                }
            ]
        }
        matches = parse_matches(payload)
        assert len(matches) == 1
        assert matches[0].home_team == "Arsenal"
        assert matches[0].competition_code == "PL"

    def test_parse_standings(self):
        payload = {
            "standings": [
                {
                    "stage": "REGULAR_SEASON",
                    "type": "TOTAL",
                    "table": [
                        {
                            "position": 1,
                            "team": {"name": "Arsenal"},
                            "points": 30,
                            "playedGames": 12,
                        },
                        {
                            "position": 2,
                            "team": {"name": "City"},
                            "points": 28,
                            "playedGames": 12,
                        },
                    ],
                }
            ]
        }
        rows = parse_standings(payload)
        assert len(rows) == 2
        assert rows[0].position == 1
        assert rows[0].points == 30

    def test_fetch_competitions_network(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"competitions": []})

        conn = FootballDataOrgConnector(
            api_token="t",
            http_client=make_client(handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        assert conn.fetch_competitions() == []

    def test_disabled_when_no_key(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_ORG_KEY", raising=False)
        conn = FootballDataOrgConnector()
        assert conn.is_enabled() is False

    def test_enabled_when_token_provided(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_ORG_KEY", raising=False)
        conn = FootballDataOrgConnector(api_token="token")
        assert conn.is_enabled() is True

    def test_disabled_collect_returns_empty(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_ORG_KEY", raising=False)
        conn = FootballDataOrgConnector()
        assert conn.collect_player_facts() == []

    def test_require_enabled_raises_when_disabled(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_ORG_KEY", raising=False)
        conn = FootballDataOrgConnector()
        with pytest.raises(DataProviderDisabledError):
            conn.fetch_competitions()


# ---------------------------------------------------------------------------
# LiveFactInjector
# ---------------------------------------------------------------------------


class TestLiveFactInjector:
    def _injector(self) -> LiveFactInjector:
        return LiveFactInjector()

    def test_fpl_chance_zero(self):
        fact = PlayerFact(
            source=FactSource.FPL_OFFICIAL, name="X", fpl_player_id=1, chance_of_playing=0
        )
        overrides = self._injector().build_overrides([fact])
        assert overrides[0].start_probability == 0.0

    def test_fpl_chance_hundred(self):
        fact = PlayerFact(
            source=FactSource.FPL_OFFICIAL, name="X", fpl_player_id=1, chance_of_playing=100
        )
        overrides = self._injector().build_overrides([fact])
        assert overrides[0].start_probability == 1.0

    def test_fpl_news_suspended(self):
        fact = PlayerFact(
            source=FactSource.FPL_OFFICIAL,
            name="X",
            fpl_player_id=1,
            news="Player is suspended for three matches.",
        )
        overrides = self._injector().build_overrides([fact])
        assert overrides[0].availability_status == "suspended"

    def test_api_football_starting(self):
        fact = PlayerFact(
            source=FactSource.API_FOOTBALL, name="X", fpl_player_id=2, is_starting=True
        )
        overrides = self._injector().build_overrides([], [fact], [])
        ov = overrides[0]
        assert ov.start_probability == 1.0
        assert ov.expected_minutes == 90

    def test_api_football_bench(self):
        fact = PlayerFact(source=FactSource.API_FOOTBALL, name="X", fpl_player_id=2, is_bench=True)
        overrides = self._injector().build_overrides([], [fact], [])
        ov = overrides[0]
        assert ov.start_probability == 0.0
        assert ov.expected_minutes == 0

    def test_api_football_injured(self):
        fact = PlayerFact(
            source=FactSource.API_FOOTBALL, name="X", fpl_player_id=2, is_injured=True
        )
        overrides = self._injector().build_overrides([], [fact], [])
        assert overrides[0].start_probability == 0.0
        assert overrides[0].availability_status == "out"

    def test_api_football_without_fpl_id_skipped(self):
        fact = PlayerFact(
            source=FactSource.API_FOOTBALL, name="X", fpl_player_id=None, is_starting=True
        )
        overrides = self._injector().build_overrides([], [fact], [])
        assert overrides == []

    def test_football_data_ignored(self):
        fact = PlayerFact(source=FactSource.FOOTBALL_DATA_ORG, name="X", fpl_player_id=3)
        overrides = self._injector().build_overrides([], [], [fact])
        assert overrides == []

    def test_api_football_overrides_fpl_for_same_player(self):
        fpl = PlayerFact(
            source=FactSource.FPL_OFFICIAL, name="X", fpl_player_id=5, chance_of_playing=100
        )
        api = PlayerFact(source=FactSource.API_FOOTBALL, name="X", fpl_player_id=5, is_bench=True)
        overrides = self._injector().build_overrides([fpl], [api], [])
        assert len(overrides) == 1
        assert overrides[0].start_probability == 0.0
        assert overrides[0].source == FactSource.API_FOOTBALL

    def test_inject_from_connectors_fpl(self):
        conn = FplOfficialConnector(
            http_client=make_client(_fpl_routing_handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        result = self._injector().inject_from_connectors(fpl=conn)
        assert result.diagnostics["fpl_official"]["enabled"] is True
        assert len(result.overrides) == 3
        by_id = {o.player_id: o for o in result.overrides}
        assert by_id[411].start_probability == 0.0
        assert by_id[411].availability_status == "suspended"
        assert by_id[615].start_probability == 1.0

    def test_inject_from_connectors_api_disabled_recorded(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        api = ApiFootballConnector()
        result = self._injector().inject_from_connectors(api_football=api)
        assert result.diagnostics["api_football"]["enabled"] is False
        assert "API_FOOTBALL_KEY not set" in result.diagnostics["api_football"]["reason"]

    def test_inject_from_connectors_fpl_failure_recorded(self):
        class _Broken:
            def collect_player_facts(self):
                raise DataConnectionError("boom")

        result = self._injector().inject_from_connectors(fpl=_Broken())
        assert "error" in result.diagnostics["fpl_official"]
        assert result.overrides == []

    def test_graceful_degradation_mixed_sources(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        fpl = FplOfficialConnector(
            http_client=make_client(_fpl_routing_handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        api = ApiFootballConnector()
        result = self._injector().inject_from_connectors(fpl=fpl, api_football=api)
        assert len(result.overrides) == 3


# ---------------------------------------------------------------------------
# FactOverrideProvider (decision bridge)
# ---------------------------------------------------------------------------


class TestFactOverrideProvider:
    def _base(self) -> StaticPredictionProvider:
        return StaticPredictionProvider(
            expected_points=5.5, expected_minutes=60.0, start_probability=0.8
        )

    def test_no_override_returns_baseline(self):
        base = self._base()
        prov = FactOverrideProvider(base, [])
        pred = prov.get_player_prediction(1, 1)
        assert pred.start_probability == 0.8
        assert pred.expected_minutes == 60.0
        assert pred.expected_points == 5.5

    def test_override_start_zero_minutes_zero(self):
        base = self._base()
        ov = FactOverride(
            player_id=1,
            source=FactSource.FPL_OFFICIAL,
            start_probability=0.0,
            expected_minutes=0.0,
        )
        prov = FactOverrideProvider(base, [ov])
        pred = prov.get_player_prediction(1, 1)
        assert pred.start_probability == 0.0
        assert pred.expected_minutes == 0.0
        assert pred.expected_points == 0.0

    def test_override_start_and_minutes_rescale_points(self):
        base = self._base()
        ov = FactOverride(
            player_id=1,
            source=FactSource.API_FOOTBALL,
            start_probability=1.0,
            expected_minutes=90.0,
        )
        prov = FactOverrideProvider(base, [ov])
        pred = prov.get_player_prediction(1, 1)
        # 5.5 * (1.0/0.8) * (90/60) = 10.3125
        assert pred.expected_points == pytest.approx(10.3125)
        assert pred.start_probability == 1.0
        assert pred.expected_minutes == 90.0

    def test_override_only_minutes(self):
        base = self._base()
        ov = FactOverride(player_id=1, source=FactSource.FPL_OFFICIAL, expected_minutes=30.0)
        prov = FactOverrideProvider(base, [ov])
        pred = prov.get_player_prediction(1, 1)
        assert pred.start_probability == 0.8
        assert pred.expected_minutes == 30.0
        assert pred.expected_points == pytest.approx(5.5 * (30.0 / 60.0))

    def test_get_all_predictions_applies(self):
        base = self._base()
        ov = FactOverride(player_id=3, source=FactSource.FPL_OFFICIAL, start_probability=0.0)
        prov = FactOverrideProvider(base, [ov])
        assert prov.get_all_predictions(1) == {}

    def test_get_squad_predictions_applies(self):
        base = self._base()
        ov = FactOverride(player_id=2, source=FactSource.FPL_OFFICIAL, start_probability=0.0)
        prov = FactOverrideProvider(base, [ov])
        out = prov.get_squad_predictions([1, 2], [1])
        assert out[1][2].start_probability == 0.0
        assert out[1][1].start_probability == 0.8

    def test_fixture_count_delegates(self):
        base = self._base()
        prov = FactOverrideProvider(base, [])
        assert prov.get_fixture_count(1, 1) == base.get_fixture_count(1, 1)

    def test_bridge_uses_overridden_provider(self):
        base = self._base()
        ov = FactOverride(
            player_id=411,
            source=FactSource.FPL_OFFICIAL,
            start_probability=0.0,
            expected_minutes=0.0,
        )
        prov = FactOverrideProvider(base, [ov])
        player_ids = [411, 615, 859] + [900 + i for i in range(12)]
        positions = {pid: (i % 4) + 1 for i, pid in enumerate(player_ids)}
        from fpl_intelligence.squad.models import SquadStateCreate

        payload = SquadStateCreate(
            player_ids=player_ids,
            captain_id=411,
            vice_captain_id=615,
            gameweek=5,
            player_positions=positions,
            player_prices={pid: 9.0 for pid in player_ids},
            player_teams={pid: 14 for pid in player_ids},
        )
        bridge = DecisionOptimizerBridge(provider=prov)
        report = bridge.generate_decisions(payload)
        # The override forces player 411's expected value to 0, so the starting
        # XI optimizer should bench them (the captain recommendation itself falls
        # back to squad.captain in Phase 10.4 and is not what we assert here).
        assert 411 not in report.starting_xi


# ---------------------------------------------------------------------------
# FactCollectionService
# ---------------------------------------------------------------------------


class TestFactCollectionService:
    def test_collect_overrides_integrates(self):
        fpl = FplOfficialConnector(
            http_client=make_client(_fpl_routing_handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        service = FactCollectionService(fpl_connector=fpl)
        result = service.collect_overrides()
        assert len(result.overrides) == 3
        assert "fpl_official" in result.diagnostics

    def test_collect_overrides_api_enabled_with_mapping(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)

        def handler(request: httpx.Request) -> httpx.Response:
            if "lineups" in request.url.path:
                return httpx.Response(200, json=_lineups_payload())
            if "fixtures" in request.url.path:
                return httpx.Response(200, json={"response": [{"fixture": {"id": 5}}]})
            return httpx.Response(200, json={"response": []})

        api = ApiFootballConnector(
            api_key="k",
            http_client=make_client(handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        service = FactCollectionService(api_football_connector=api)
        result = service.collect_overrides(date="2026-08-22", fpl_id_map={900: 411, 902: 859})
        ids = {o.player_id for o in result.overrides}
        assert 411 in ids

    def test_index_overrides_helper(self):
        from fpl_intelligence.data_providers import index_overrides

        ov = FactOverride(player_id=1, source=FactSource.FPL_OFFICIAL)
        out = index_overrides([ov])
        assert out[1] is ov


# ---------------------------------------------------------------------------
# Decisions endpoint — live fact integration & fallback
# ---------------------------------------------------------------------------


def _squad_payload() -> dict:
    player_ids = [411, 615, 859] + [900 + i for i in range(12)]
    positions = {pid: (i % 4) + 1 for i, pid in enumerate(player_ids)}
    return {
        "session_id": "livefacts-test",
        "player_ids": player_ids,
        "captain_id": 411,
        "vice_captain_id": 615,
        "gameweek": 5,
        "player_positions": positions,
        "player_prices": {pid: 9.0 for pid in player_ids},
        "player_teams": {pid: 14 for pid in player_ids},
    }


@pytest.fixture
def api_client(db_session):
    from fastapi.testclient import TestClient
    from sqlalchemy import delete

    from fpl_intelligence.api import deps
    from fpl_intelligence.api.main import app
    from fpl_intelligence.live_intelligence.bridge import StaticPredictionProvider
    from fpl_intelligence.squad.models_db import SquadStateDB

    # Phase 11.2 — squad state is DB-backed; start each test clean.
    db_session.execute(delete(SquadStateDB))
    db_session.commit()
    app.dependency_overrides[deps._get_db_session] = lambda: db_session
    app.dependency_overrides[deps.get_prediction_provider] = lambda: StaticPredictionProvider()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        db_session.execute(delete(SquadStateDB))
        db_session.commit()


class TestDecisionsEndpointLiveFacts:
    def test_baseline_when_live_facts_false(self, api_client):
        api_client.post("/api/v1/squad", json=_squad_payload())
        resp = api_client.get(
            "/api/v1/decisions",
            params={"live_facts": "false", "session_id": "livefacts-test"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["live_facts_applied"] == 0

    def test_live_facts_applied_via_injected_service(self, api_client, monkeypatch):
        from fpl_intelligence.api.routes import squad as squad_route

        api_client.post("/api/v1/squad", json=_squad_payload())

        fake = type(
            "S",
            (),
            {
                "collect_overrides": lambda self: LiveFactResult(
                    overrides=[
                        FactOverride(
                            player_id=411,
                            source=FactSource.FPL_OFFICIAL,
                            start_probability=0.0,
                            expected_minutes=0.0,
                        )
                    ],
                    by_player={411: FactOverride(player_id=411, source=FactSource.FPL_OFFICIAL)},
                    diagnostics={"fpl_official": {"enabled": True}},
                )
            },
        )()
        monkeypatch.setattr(squad_route, "FactCollectionService", lambda: fake)
        resp = api_client.get(
            "/api/v1/decisions",
            params={"live_facts": "true", "session_id": "livefacts-test"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["live_facts_applied"] == 1
        assert "fpl_official" in body["meta"]["live_fact_sources"]

    def test_live_facts_failure_falls_back(self, api_client, monkeypatch):
        from fpl_intelligence.api.routes import squad as squad_route

        api_client.post("/api/v1/squad", json=_squad_payload())

        def boom(self):
            raise RuntimeError("upstream down")

        fake = type("S", (), {"collect_overrides": boom})()
        monkeypatch.setattr(squad_route, "FactCollectionService", lambda: fake)
        resp = api_client.get(
            "/api/v1/decisions",
            params={"live_facts": "true", "session_id": "livefacts-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["live_facts_applied"] == 0

    def test_missing_squad_returns_404(self, api_client):
        resp = api_client.get("/api/v1/decisions")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# CLI script
# ---------------------------------------------------------------------------


class TestFetchLiveFactsCLI:
    def test_parse_args_defaults(self):
        args = parse_args([])
        assert args.dry_run is False
        assert args.cache_dir is None
        assert args.date is None

    def test_parse_args_full(self):
        args = parse_args(["--date", "2026-08-22", "--cache-dir", "/tmp/x", "--dry-run"])
        assert args.date == "2026-08-22"
        assert args.cache_dir == "/tmp/x"
        assert args.dry_run is True

    def test_summarize_renders_overrides(self):
        result = LiveFactResult(
            overrides=[
                FactOverride(
                    player_id=1,
                    source=FactSource.FPL_OFFICIAL,
                    start_probability=0.0,
                    availability_status="suspended",
                )
            ],
            by_player={1: FactOverride(player_id=1, source=FactSource.FPL_OFFICIAL)},
            diagnostics={"fpl_official": {"enabled": True, "facts": 3}},
        )
        text = summarize(result, 3)
        assert "Phase 11.1 Live Fact Summary" in text
        assert "Hard fact overrides discovered: 1" in text
        assert "suspended" in text

    def test_run_with_fake_service_no_network(self, tmp_path):
        fpl = FplOfficialConnector(
            http_client=make_client(_fpl_routing_handler),
            clock=lambda: 0.0,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        service = FactCollectionService(fpl_connector=fpl)
        args = parse_args(["--dry-run"])
        code = run(args, service=service)
        assert code == 0
