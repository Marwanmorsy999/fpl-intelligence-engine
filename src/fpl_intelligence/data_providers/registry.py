"""Central registry and fallback policy for external data providers."""

from __future__ import annotations

import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ProviderState(StrEnum):
    """The state in which a provider result was obtained."""

    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    CACHE = "CACHE"
    STALE_DATA = "STALE_DATA"
    UNAVAILABLE = "UNAVAILABLE"


class ProviderHealthState(StrEnum):
    """Operational state tracked for a registered provider."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH_FAILED = "AUTH_FAILED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    STALE_ONLY = "STALE_ONLY"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class ProviderMetadata:
    """Operational and temporal contract for one provider."""

    name: str
    capabilities: tuple[str, ...] = ()
    enabled: bool = True
    priority: int = 100
    cost_tier: str = "free"
    quota: int | None = None
    per_minute_limit: int | None = None
    freshness: str = "unknown"
    cache_ttl_seconds: int = 900
    reliability: float = 0.0
    temporal_safety: str = "UNKNOWN"
    terms_permission: str = "unknown"
    request_window_seconds: float | None = None
    last_reset_at: float | None = None


@dataclass(frozen=True)
class ProviderBudget:
    requests_used: int = 0
    requests_remaining: int | None = None
    request_window_seconds: float | None = None
    provider_limit: int | None = None
    last_reset_at: float | None = None


@dataclass
class _ProviderRuntime:
    health: ProviderHealthState = ProviderHealthState.HEALTHY
    requests_used: int = 0
    window_started_at: float = 0.0
    last_reset_at: float | None = None


@dataclass
class ProviderResult:
    """A provider result with an auditable fallback state."""

    state: ProviderState
    provider: str | None = None
    value: Any = None
    errors: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    temporal: dict[str, Any] = field(default_factory=dict)

    @property
    def stale(self) -> bool:
        return self.state is ProviderState.STALE_DATA


@dataclass
class _RegisteredProvider:
    metadata: ProviderMetadata
    provider: Any


class ProviderRegistry:
    """Register providers once and resolve them in priority order."""

    def __init__(
        self,
        providers: Iterable[tuple[Any, ProviderMetadata]] = (),
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._providers: dict[str, _RegisteredProvider] = {}
        self._runtime: dict[str, _ProviderRuntime] = {}
        self._lock = threading.RLock()
        self._clock = clock
        for provider, metadata in providers:
            self.register(provider, metadata)

    def register(self, provider: Any, metadata: ProviderMetadata) -> None:
        if not metadata.name.strip():
            raise ValueError("provider name must not be empty")
        if metadata.name in self._providers:
            raise ValueError(f"provider already registered: {metadata.name}")
        if metadata.cache_ttl_seconds < 0:
            raise ValueError("cache TTL must be non-negative")
        self._providers[metadata.name] = _RegisteredProvider(metadata, provider)
        self._runtime[metadata.name] = _ProviderRuntime(window_started_at=self._clock())

    def budget(self, name: str) -> ProviderBudget:
        """Return a snapshot of the provider's process-local request budget."""
        metadata = self.metadata(name)
        with self._lock:
            runtime = self._runtime[name]
            self._reset_window_if_needed(metadata, runtime)
            remaining = (
                max(0, metadata.quota - runtime.requests_used)
                if metadata.quota is not None
                else None
            )
            return ProviderBudget(
                requests_used=runtime.requests_used,
                requests_remaining=remaining,
                request_window_seconds=metadata.request_window_seconds,
                provider_limit=metadata.quota,
                last_reset_at=metadata.last_reset_at or runtime.last_reset_at,
            )

    def health(self, name: str) -> ProviderHealthState:
        metadata = self.metadata(name)
        if not metadata.enabled:
            return ProviderHealthState.DISABLED
        with self._lock:
            return self._runtime[name].health

    def set_health(self, name: str, state: ProviderHealthState) -> None:
        self.metadata(name)
        with self._lock:
            self._runtime[name].health = state

    def _reset_window_if_needed(
        self, metadata: ProviderMetadata, runtime: _ProviderRuntime
    ) -> None:
        window = metadata.request_window_seconds
        if window and self._clock() - runtime.window_started_at >= window:
            runtime.requests_used = 0
            runtime.window_started_at = self._clock()
            runtime.last_reset_at = runtime.window_started_at

    def _reserve_request(self, metadata: ProviderMetadata) -> bool:
        with self._lock:
            runtime = self._runtime[metadata.name]
            self._reset_window_if_needed(metadata, runtime)
            if metadata.quota is not None and runtime.requests_used >= metadata.quota:
                runtime.health = ProviderHealthState.QUOTA_EXHAUSTED
                return False
            runtime.requests_used += 1
            return True

    def metadata(self, name: str) -> ProviderMetadata:
        try:
            return self._providers[name].metadata
        except KeyError as exc:
            raise KeyError(f"unknown provider: {name}") from exc

    def provider(self, name: str) -> Any:
        try:
            return self._providers[name].provider
        except KeyError as exc:
            raise KeyError(f"unknown provider: {name}") from exc

    def ordered(self, *, capability: str | None = None) -> list[ProviderMetadata]:
        providers = [entry.metadata for entry in self._providers.values()]
        if capability is not None:
            providers = [p for p in providers if capability in p.capabilities]
        return sorted(providers, key=lambda p: p.priority)

    def resolve(
        self,
        fetch: Callable[[Any], Any],
        *,
        capability: str | None = None,
        cached: Callable[[], Any] | None = None,
        stale: Callable[[], Any] | None = None,
    ) -> ProviderResult:
        """Return cache first, then try budgeted providers and fallback data."""
        errors: list[str] = []
        eligible = [
            metadata
            for metadata in self.ordered(capability=capability)
            if metadata.enabled
        ]
        if cached is not None:
            try:
                value = cached()
            except Exception as exc:
                errors.append(f"cache: {exc}")
            else:
                if value is not None:
                    return ProviderResult(
                        ProviderState.CACHE, provider="cache", value=value, errors=errors
                    )
        for index, metadata in enumerate(eligible):
            if not self._reserve_request(metadata):
                errors.append(f"{metadata.name}: quota exhausted")
                continue
            try:
                value = fetch(self._providers[metadata.name].provider)
            except Exception as exc:  # provider failures must not cascade
                errors.append(f"{metadata.name}: {exc}")
                with self._lock:
                    self._runtime[metadata.name].health = ProviderHealthState.DEGRADED
                continue
            if value is not None:
                with self._lock:
                    self._runtime[metadata.name].health = ProviderHealthState.HEALTHY
                state = ProviderState.PRIMARY if index == 0 else ProviderState.SECONDARY
                return ProviderResult(state, metadata.name, value, errors)
        if stale is not None:
            try:
                value = stale()
            except Exception as exc:
                errors.append(f"stale: {exc}")
            else:
                if value is not None:
                    return ProviderResult(
                        ProviderState.STALE_DATA, provider="stale", value=value, errors=errors
                    )
        return ProviderResult(ProviderState.UNAVAILABLE, errors=errors)

    async def resolve_async(
        self,
        fetch: Callable[[Any], Awaitable[Any]],
        *,
        capability: str | None = None,
        cached: Callable[[], Awaitable[Any]] | None = None,
        stale: Callable[[], Awaitable[Any]] | None = None,
    ) -> ProviderResult:
        """Async equivalent of ``resolve`` sharing budget and health state."""
        errors: list[str] = []
        eligible = [
            metadata
            for metadata in self.ordered(capability=capability)
            if metadata.enabled
        ]
        if cached is not None:
            try:
                value = await cached()
            except Exception as exc:
                errors.append(f"cache: {exc}")
            else:
                if value is not None:
                    return ProviderResult(
                        ProviderState.CACHE, provider="cache", value=value, errors=errors
                    )
        for index, metadata in enumerate(eligible):
            if not self._reserve_request(metadata):
                errors.append(f"{metadata.name}: quota exhausted")
                continue
            try:
                value = await fetch(self._providers[metadata.name].provider)
            except Exception as exc:  # provider failures must not cascade
                errors.append(f"{metadata.name}: {exc}")
                with self._lock:
                    self._runtime[metadata.name].health = ProviderHealthState.DEGRADED
                continue
            if value is not None:
                with self._lock:
                    self._runtime[metadata.name].health = ProviderHealthState.HEALTHY
                state = ProviderState.PRIMARY if index == 0 else ProviderState.SECONDARY
                return ProviderResult(state, metadata.name, value, errors)
        if stale is not None:
            try:
                value = await stale()
            except Exception as exc:
                errors.append(f"stale: {exc}")
            else:
                if value is not None:
                    return ProviderResult(
                        ProviderState.STALE_DATA, provider="stale", value=value, errors=errors
                    )
        return ProviderResult(ProviderState.UNAVAILABLE, errors=errors)


class FplProviderAdapter:
    """Registry-backed facade for legacy synchronous FPL ingestion providers."""

    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.RLock()

    def _fetch(self, method: str, cache_key: str) -> Any:
        provider_method = method

        def fetch(provider: Any) -> Any:
            if hasattr(provider, provider_method):
                return getattr(provider, provider_method)()
            legacy_method = {
                "fetch_bootstrap": "get_bootstrap_static",
                "fetch_fixtures": "get_fixtures",
            }[provider_method]
            return getattr(provider, legacy_method)()

        with self._cache_lock:
            cached_entry = self._cache.get(cache_key)
            cached = None
            if cached_entry is not None:
                stored_at, cached_value = cached_entry
                ttl = self.registry.metadata("fpl_official").cache_ttl_seconds
                if time.monotonic() - stored_at <= ttl:
                    cached = cached_value
                else:
                    self._cache.pop(cache_key, None)
            result = self.registry.resolve(
                fetch,
                capability="players" if "bootstrap" in method else "fixtures",
                cached=lambda: cached,
            )
            if result.state is ProviderState.UNAVAILABLE:
                raise RuntimeError("FPL provider unavailable: " + "; ".join(result.errors))
            if result.state is not ProviderState.CACHE:
                self._cache[cache_key] = (time.monotonic(), result.value)
            return result.value

    def get_bootstrap_static(self) -> Any:
        return self._fetch("fetch_bootstrap", "/api/bootstrap-static/")

    def get_fixtures(self) -> Any:
        return self._fetch("fetch_fixtures", "/api/fixtures/")


@dataclass(frozen=True)
class _AsyncFetchValue:
    value: Any
    provenance: dict[str, Any]


class AsyncFplProviderAdapter:
    """Async FPL facade; egress and provider policy stay behind this boundary."""

    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry
        self._cache: dict[str, tuple[float, ProviderResult]] = {}
        self._locks: dict[str, Any] = {}
        self._last_result: ProviderResult | None = None

    @property
    def last_result(self) -> ProviderResult | None:
        return self._last_result

    @property
    def winning_strategy(self) -> str | None:
        """Expose the winning egress strategy for legacy importer reporting."""
        if self._last_result is None:
            return None
        return self._last_result.provenance.get("egress_strategy")

    def _lock_for(self, cache_key: str) -> Any:
        import asyncio

        if cache_key not in self._locks:
            self._locks[cache_key] = asyncio.Lock()
        return self._locks[cache_key]

    async def resolve(
        self,
        path: str,
        *,
        validator: Callable[[Any], None] | None = None,
        capability: str = "fpl",
        timeout: float | None = None,
        use_cache: bool = True,
        stale: Callable[[], Awaitable[Any]] | None = None,
    ) -> ProviderResult:
        """Fetch cache-first through the registry and return audit metadata."""
        cache_key = path if path.startswith("/") else f"/{path}"
        async with self._lock_for(cache_key):
            now = time.monotonic()
            cached = self._cache.get(cache_key)
            eligible = self.registry.ordered(capability=capability)
            ttl = self.registry.metadata(eligible[0].name).cache_ttl_seconds if eligible else 0
            if use_cache and cached is not None and now - cached[0] <= ttl:
                result = ProviderResult(
                    ProviderState.CACHE,
                    provider="cache",
                    value=cached[1].value,
                    provenance={**cached[1].provenance, "served_from": "cache"},
                    temporal={**cached[1].temporal, "cached_at": datetime.now(UTC).isoformat()},
                )
                self._last_result = result
                return result

            async def fetch(provider: Any) -> _AsyncFetchValue:
                chain = provider(timeout) if callable(provider) else provider
                payload = await chain.fetch(cache_key, validator=validator)
                return _AsyncFetchValue(
                    payload,
                    {
                        "endpoint": cache_key,
                        "egress_strategy": getattr(chain, "winning_strategy", None) or "direct",
                    },
                )

            async def empty_cache() -> None:
                return None

            result = await self.registry.resolve_async(
                fetch, capability=capability, cached=empty_cache, stale=stale
            )
            if isinstance(result.value, _AsyncFetchValue):
                fetched = result.value
                result.value = fetched.value
                result.provenance = {"provider": result.provider, **fetched.provenance}
                if result.provider:
                    metadata = self.registry.metadata(result.provider)
                    result.temporal = {
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "freshness": metadata.freshness,
                        "temporal_safety": metadata.temporal_safety,
                    }
                if use_cache and result.state is not ProviderState.STALE_DATA:
                    self._cache[cache_key] = (time.monotonic(), result)
            self._last_result = result
            return result

    async def fetch(self, path: str, **kwargs: Any) -> Any:
        """Compatibility convenience returning only the payload."""
        result = await self.resolve(path, **kwargs)
        if result.state is ProviderState.UNAVAILABLE:
            from fpl_intelligence.data_providers.fpl_egress import FplEgressExhaustedError

            raise FplEgressExhaustedError(
                path,
                [("registry", error) for error in result.errors],
            )
        return result.value


def build_async_fpl_registry(
    *, settings: Any = None, egress_factory: Callable[[float | None], Any] | None = None
) -> ProviderRegistry:
    """Build the async FPL registry without performing network I/O."""
    if settings is None:
        from fpl_intelligence.config import get_settings

        settings = get_settings()
    if egress_factory is None:
        from fpl_intelligence.data_providers.fpl_egress import FplEgressChain

        def egress_factory(timeout: float | None = None) -> Any:
            return FplEgressChain(
                settings.fpl_base_url,
                timeout=timeout if timeout is not None else settings.egress_strategy_timeout,
                cache_ttl=settings.egress_cache_ttl,
            )
    return ProviderRegistry(
        [
            (
                egress_factory,
                ProviderMetadata(
                    name="fpl_egress",
                    capabilities=("fpl", "players", "fixtures", "availability"),
                    priority=10,
                    freshness="near-live",
                    cache_ttl_seconds=settings.egress_cache_ttl,
                    reliability=0.95,
                    temporal_safety="LIVE_ONLY",
                    terms_permission="public-api-review-required",
                ),
            )
        ]
    )


def async_fpl_adapter(
    *, registry: ProviderRegistry | None = None, settings: Any = None
) -> AsyncFplProviderAdapter:
    """Construct the async FPL facade used by application services."""
    return AsyncFplProviderAdapter(registry or build_async_fpl_registry(settings=settings))


_DEFAULT_ASYNC_ADAPTER: AsyncFplProviderAdapter | None = None


def get_async_fpl_adapter(*, settings: Any = None) -> AsyncFplProviderAdapter:
    """Return the process-local async adapter so cache and health are shared."""
    global _DEFAULT_ASYNC_ADAPTER
    if _DEFAULT_ASYNC_ADAPTER is None:
        _DEFAULT_ASYNC_ADAPTER = async_fpl_adapter(settings=settings)
    return _DEFAULT_ASYNC_ADAPTER


def fpl_ingestion_adapter(
    provider: Any = None,
    *,
    registry: ProviderRegistry | None = None,
    provider_factory: Callable[[], Any] | None = None,
) -> FplProviderAdapter:
    """Build the sole FPL ingestion facade; legacy providers remain internal."""
    if registry is None:
        if provider is None:
            if provider_factory is not None:
                provider = provider_factory()
            else:
                registry = build_default_registry()
        if registry is None:
            assert provider is not None
            registry = ProviderRegistry(
                [
                    (
                        provider,
                        ProviderMetadata(
                            name="fpl_official",
                            capabilities=("players", "fixtures", "availability"),
                            priority=10,
                        ),
                    )
                ]
            )
    return FplProviderAdapter(registry)


def build_default_registry(
    *,
    fpl: Any = None,
    api_football: Any = None,
    open_meteo: Any = None,
) -> ProviderRegistry:
    """Build the standard registry without making network calls."""
    if fpl is None:
        from fpl_intelligence.data_providers.fpl_official import FplOfficialConnector

        fpl = FplOfficialConnector()
    if api_football is None:
        from fpl_intelligence.data_providers.api_football import ApiFootballConnector

        api_football = ApiFootballConnector()
    if open_meteo is None:
        from fpl_intelligence.data_providers.open_meteo import OpenMeteoConnector

        open_meteo = OpenMeteoConnector()
    return ProviderRegistry(
        [
            (
            fpl,
            ProviderMetadata(
                name="fpl_official",
                capabilities=("players", "fixtures", "availability"),
                priority=10,
                freshness="near-live",
                cache_ttl_seconds=900,
                reliability=0.95,
                temporal_safety="LIVE_ONLY",
                terms_permission="public-api-review-required",
            ),
            ),
            (
            api_football,
            ProviderMetadata(
                name="api_football",
                capabilities=("fixtures", "lineups", "injuries"),
                enabled=api_football.is_enabled(),
                priority=20,
                quota=100,
                per_minute_limit=10,
                request_window_seconds=24 * 3600,
                freshness="near-live",
                cache_ttl_seconds=60,
                reliability=0.85,
                temporal_safety="LIVE_ONLY",
                terms_permission="keyed-free-tier",
            ),
            ),
            (
            open_meteo,
            ProviderMetadata(
                name="open_meteo",
                capabilities=("weather",),
                priority=30,
                freshness="forecast",
                cache_ttl_seconds=6 * 3600,
                reliability=0.8,
                temporal_safety="FORECAST_ONLY",
                terms_permission="public-api-review-required",
            ),
            ),
        ]
    )