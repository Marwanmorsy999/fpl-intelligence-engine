"""Phase 11.1 — Base class for structured external-data connectors.

Every API-first connector (official FPL, API-Football, football-data.org)
shares the same plumbing here: an injectable ``httpx.Client`` (so tests never
touch the network), a shared :class:`ResponseCache`, polite rate limiting via
the existing Phase 9.1 :class:`RateLimiter`, and a typed error hierarchy.

The one public data method subclasses call is :meth:`BaseDataConnector._get_json`,
which is cache-first: a cache hit returns immediately and never reaches the
network, a rate-limit pause happens *before* the request, and HTTP/parse
failures surface as typed errors the caller decides how to handle.

Credentials and endpoints are never hardcoded: keyed connectors read their key
from an environment variable (or a constructor argument) only.
"""

from __future__ import annotations

import logging
import time
from abc import ABC
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from fpl_intelligence.data_providers.cache import ResponseCache
from fpl_intelligence.live_intelligence.rate_limit import (
    MonotonicClock,
    RateLimiter,
    SleepFn,
)

logger = logging.getLogger(__name__)

#: Client identification; some APIs reject clients without a User-Agent.
DEFAULT_USER_AGENT = "fpl-intelligence-engine/1.0 (api-first-data-providers)"


class DataConnectorError(RuntimeError):
    """Base class for every data-connector failure."""


class DataConnectionError(DataConnectorError):
    """The source could not be reached (network / HTTP / rate-limit failure)."""


class DataParseError(DataConnectorError):
    """The source returned a payload that could not be parsed."""


class DataProviderDisabledError(DataConnectorError):
    """The connector is disabled (e.g. required API key missing)."""


class BaseDataConnector(ABC):  # noqa: B024 - shared plumbing, not a standalone contract
    """Shared HTTP + cache + rate-limit plumbing for a single data source."""

    #: Short, stable machine key used for logging and diagnostics.
    name: str = "base"

    def __init__(
        self,
        *,
        cache: ResponseCache | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
        min_interval_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        monotonic_clock: MonotonicClock = time.monotonic,
        sleep: SleepFn = time.sleep,
    ) -> None:
        self._cache = cache or ResponseCache()
        self._http_client = http_client
        self._owns_client = http_client is None
        self._timeout = timeout
        self._headers = dict(headers or {})
        self._headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
        self._rate = RateLimiter(min_interval_seconds, clock=monotonic_clock, sleep=sleep)

    @property
    def cache(self) -> ResponseCache:
        return self._cache

    @property
    def rate_limiter(self) -> RateLimiter:
        return self._rate

    def is_enabled(self) -> bool:
        """Whether this connector can make live calls. Defaults to True."""
        return True

    # -- low-level HTTP -----------------------------------------------------

    def _lazy_client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(headers=self._headers, timeout=self._timeout)
            self._owns_client = True
        return self._http_client

    def close(self) -> None:
        """Release a client this connector lazily created; injected ones are kept."""
        if self._owns_client and self._http_client is not None:
            self._http_client.close()
        self._http_client = None
        self._owns_client = False

    def _get_json(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        *,
        sensitive: bool = False,
    ) -> Any:
        """Cache-first GET returning parsed JSON, or a typed error.

        Rate limiting happens *before* the request so a burst can never trigger
        a 429. On any HTTP/parse failure a typed :class:`DataConnectorError` is
        raised — the caller (orchestrator, injector) decides what to do.
        """
        cached = self._cache.get(endpoint, dict(params) if params else None, sensitive=sensitive)
        if cached is not None:
            return cached

        self._rate.acquire()
        client = self._lazy_client()
        try:
            response = client.get(
                endpoint,
                params=params,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise DataConnectionError(f"GET {endpoint} failed: {exc}") from exc

        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise DataConnectionError(f"source rate-limited (429): {endpoint}")
        if response.is_error:
            raise DataConnectionError(f"GET {endpoint} -> HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataParseError(f"{self.name} payload is not valid JSON: {exc}") from exc

        self._cache.store(endpoint, dict(params) if params else None, payload, sensitive=sensitive)
        return payload
