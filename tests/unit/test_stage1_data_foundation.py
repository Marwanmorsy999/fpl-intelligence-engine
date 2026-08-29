import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx

from fpl_intelligence.availability.historical.temporal import (
    AvailabilityTimestamps,
    classify_and_check_eligibility,
)
from fpl_intelligence.availability.models import TemporalClass
from fpl_intelligence.data_providers import (
    AsyncFplProviderAdapter,
    FactSource,
    LiveFactInjector,
    OpenMeteoConnector,
    PlayerFact,
    ProviderHealthState,
    ProviderMetadata,
    ProviderRegistry,
    ProviderState,
    ResponseCache,
    fpl_ingestion_adapter,
)


def test_async_adapter_cache_quota_provenance_and_temporal_metadata():
    calls = 0

    class Chain:
        winning_strategy = "allorigins"

        async def fetch(self, _path, *, validator=None):
            nonlocal calls
            calls += 1
            value = {"elements": []}
            if validator:
                validator(value)
            return value

    registry = ProviderRegistry(
        [
            (
                lambda _timeout=None: Chain(),
                ProviderMetadata(
                    name="fpl_egress",
                    capabilities=("fpl",),
                    quota=1,
                    freshness="near-live",
                    temporal_safety="LIVE_ONLY",
                ),
            )
        ]
    )
    adapter = AsyncFplProviderAdapter(registry)

    async def exercise():
        first = await adapter.resolve("/api/bootstrap-static/")
        second = await adapter.resolve("/api/bootstrap-static/")
        refused = await adapter.resolve("/api/other/")
        return first, second, refused

    first, second, refused = asyncio.run(exercise())
    assert first.value == {"elements": []}
    assert first.provenance["egress_strategy"] == "allorigins"
    assert first.temporal["temporal_safety"] == "LIVE_ONLY"
    assert second.state is ProviderState.CACHE
    assert calls == 1
    assert refused.state is ProviderState.UNAVAILABLE
    assert registry.health("fpl_egress") is ProviderHealthState.QUOTA_EXHAUSTED


def test_async_adapter_fallback_health_and_concurrent_requests():
    calls: list[str] = []

    class BrokenChain:
        winning_strategy = "direct"

        async def fetch(self, _path, *, validator=None):
            calls.append("broken")
            raise RuntimeError("blocked")

    class HealthyChain:
        winning_strategy = "env_proxy"

        async def fetch(self, _path, *, validator=None):
            calls.append("healthy")
            await asyncio.sleep(0)
            return {"picks": []}

    registry = ProviderRegistry(
        [
            (
                lambda _timeout=None: BrokenChain(),
                ProviderMetadata(name="primary", priority=10, capabilities=("fpl",)),
            ),
            (
                lambda _timeout=None: HealthyChain(),
                ProviderMetadata(name="secondary", priority=20, capabilities=("fpl",)),
            ),
        ]
    )
    adapter = AsyncFplProviderAdapter(registry)

    async def exercise():
        return await asyncio.gather(
            adapter.resolve("/api/entry/1/event/1/picks/"),
            adapter.resolve("/api/entry/1/event/1/picks/"),
        )

    first, second = asyncio.run(exercise())
    assert first.state is ProviderState.SECONDARY
    assert second.state is ProviderState.CACHE
    assert first.provenance["egress_strategy"] == "env_proxy"
    assert registry.health("primary") is ProviderHealthState.DEGRADED
    assert registry.health("secondary") is ProviderHealthState.HEALTHY
    assert calls == ["broken", "healthy"]


def test_registry_budget_refuses_calls_and_resets_window():
    now = [0.0]
    calls = 0

    def fetch(_provider):
        nonlocal calls
        calls += 1
        return {"ok": True}

    registry = ProviderRegistry(
        [("limited", ProviderMetadata(name="limited", quota=1, request_window_seconds=10))],
        clock=lambda: now[0],
    )
    assert registry.resolve(fetch).value == {"ok": True}
    assert registry.resolve(fetch, stale=lambda: {"stale": True}).value == {"stale": True}
    assert calls == 1
    assert registry.health("limited") is ProviderHealthState.QUOTA_EXHAUSTED
    assert registry.budget("limited").requests_remaining == 0
    now[0] = 11.0
    assert registry.resolve(fetch).value == {"ok": True}
    assert registry.budget("limited").requests_used == 1


def test_fpl_adapter_cache_prevents_duplicate_concurrent_requests():
    class Provider:
        calls = 0

        def fetch_bootstrap(self):
            Provider.calls += 1
            return {"elements": []}

    registry = ProviderRegistry(
        [(
            Provider(),
            ProviderMetadata(name="fpl_official", capabilities=("players",)),
        )]
    )
    adapter = fpl_ingestion_adapter(registry=registry)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: adapter.get_bootstrap_static(), range(2)))
    assert results == [{"elements": []}, {"elements": []}]
    assert Provider.calls == 1


def test_registry_falls_back_in_priority_order_and_reports_state():
    registry = ProviderRegistry(
        [
            ("primary", ProviderMetadata(name="primary", priority=10)),
            ("secondary", ProviderMetadata(name="secondary", priority=20)),
        ]
    )
    attempted = []

    def fetch(provider):
        attempted.append(provider)
        return "value" if provider == "secondary" else None

    result = registry.resolve(fetch)

    assert result.state is ProviderState.SECONDARY
    assert result.provider == "secondary"
    assert result.value == "value"
    assert attempted == ["primary", "secondary"]


def test_registry_reaches_stale_data_after_provider_and_cache_fail():
    registry = ProviderRegistry(
        [("primary", ProviderMetadata(name="primary", priority=10))]
    )

    result = registry.resolve(
        lambda _provider: (_ for _ in ()).throw(RuntimeError("offline")),
        cached=lambda: None,
        stale=lambda: {"value": 1},
    )

    assert result.state is ProviderState.STALE_DATA
    assert result.stale
    assert result.value == {"value": 1}
    assert result.errors == ["primary: offline"]


def test_registry_skips_disabled_provider_when_marking_secondary():
    registry = ProviderRegistry(
        [
            ("disabled", ProviderMetadata(name="disabled", priority=10, enabled=False)),
            ("secondary", ProviderMetadata(name="secondary", priority=20)),
        ]
    )

    result = registry.resolve(lambda provider: "ok" if provider == "secondary" else None)

    assert result.state is ProviderState.PRIMARY
    assert result.provider == "secondary"


def test_registry_preserves_cache_and_stale_provenance():
    registry = ProviderRegistry(
        [("primary", ProviderMetadata(name="primary", priority=10))]
    )

    cached = registry.resolve(lambda _provider: None, cached=lambda: "cached")
    stale = registry.resolve(
        lambda _provider: (_ for _ in ()).throw(RuntimeError("offline")),
        stale=lambda: "stale",
    )

    assert cached.state is ProviderState.CACHE
    assert cached.provider == "cache"
    assert stale.state is ProviderState.STALE_DATA
    assert stale.provider == "stale"


def test_provider_metadata_and_temporal_envelope_survive_orchestration():
    registry = ProviderRegistry(
        [("fpl", ProviderMetadata(name="fpl", quota=None, temporal_safety="LIVE_ONLY"))]
    )
    assert registry.metadata("fpl").temporal_safety == "LIVE_ONLY"

    published_at = datetime(2026, 8, 28, 10, tzinfo=UTC)
    available_at = datetime(2026, 8, 28, 11, tzinfo=UTC)
    fact = PlayerFact(
        source=FactSource.FPL_OFFICIAL,
        name="Player",
        fpl_player_id=1,
        fetched_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
        published_at=published_at,
        available_at=available_at,
        temporal_class="PRE_DEADLINE_BUT_UNCERTAIN",
        chance_of_playing=50,
    )

    override = LiveFactInjector().build_overrides([fact], [])[0]

    assert override.published_at == published_at
    assert override.available_at == available_at
    assert override.temporal_class == "PRE_DEADLINE_BUT_UNCERTAIN"
    assert "fetched_at" in fact.to_dict()


def test_open_meteo_uses_central_cache():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "daily": {
                    "time": ["2026-08-28"],
                    "precipitation_sum": [0],
                    "wind_speed_10m_max": [10],
                }
            },
        )

    cache = ResponseCache(clock=lambda: 0)
    connector = OpenMeteoConnector(
        cache=cache,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert connector.fetch_matchday_outlook(1, target_date="2026-08-28") is not None
    assert connector.fetch_matchday_outlook(1, target_date="2026-08-28") is not None
    assert calls == 1


def test_strict_temporal_path_rejects_explicit_lookahead():
    timestamps = AvailabilityTimestamps(
        published_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
        available_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
    )
    temporal_class, eligible = classify_and_check_eligibility(
        timestamps,
        datetime(2026, 8, 27, 12, tzinfo=UTC),
        unsafe_lookahead=True,
    )

    assert temporal_class is TemporalClass.UNSAFE_LOOKAHEAD
    assert not eligible
    assert TemporalClass.PRE_DEADLINE_BUT_UNCERTAIN is TemporalClass.HISTORICAL_EVENT_ONLY
    assert TemporalClass.POST_DEADLINE_OUTCOME_ONLY is TemporalClass.OUTCOME_ONLY
