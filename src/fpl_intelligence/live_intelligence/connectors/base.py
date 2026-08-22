"""Phase 9.5 — Live Source Connector abstraction (:mod:`base`).

Defines the :class:`SourceConnector` interface every live news / API fetcher
implements, a small typed exception hierarchy, and the shared HTTP + rate-limit
plumbing that all concrete connectors reuse.

Design rules
------------
* A connector returns a list of :class:`~fpl_intelligence.live_intelligence.raw_item_ledger.RawItem`
  objects — the same canonical ingested-content model used by Phase 9.2 — so a
  fetched item can be handed straight to ``ingest_raw_text`` with no type
  conversion and no loss of provenance.
* A connector never blocks on a network error: HTTP failures surface as
  :class:`SourceConnectionError`, malformed payloads as :class:`SourceParseError`.
  It is the orchestrator's decision what to do with them, not the connector's.
* Pacing is delegated to the existing Phase 9.1 :class:`RateLimiter`. Tests
  inject fake clock / sleep functions so the exact pacing decision is asserted
  without any wall-clock delay, keeping the suite offline and fast.
* Credentials and endpoints are never hardcoded: connectors read URLs (and, where
  required, API keys) from constructor arguments or environment variables only.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from fpl_intelligence.live_intelligence.rate_limit import (
    MonotonicClock,
    RateLimiter,
    SleepFn,
)
from fpl_intelligence.live_intelligence.raw_item_ledger import RawItem
from fpl_intelligence.live_intelligence.source_registry import SourceType
from fpl_intelligence.live_intelligence.temporal_ledger import Clock, utc_now

#: Default client identification. Some football APIs reject clients that do not
#: identify themselves with a User-Agent; this is identification, not a key.
DEFAULT_USER_AGENT = "fpl-intelligence-engine/0.9.5 (live-source-connectors)"


class SourceConnectorError(RuntimeError):
    """Base class for every source connector failure."""


class SourceConnectionError(SourceConnectorError):
    """The source could not be reached (network / HTTP / rate-limit failure)."""


class SourceParseError(SourceConnectorError):
    """The source returned a payload that could not be parsed."""


class SourceConnector(ABC):
    """Interface + shared plumbing for a single live news / data source.

    Subclasses implement :meth:`fetch`, which returns a list of
    :class:`~fpl_intelligence.live_intelligence.raw_item_ledger.RawItem`
    objects. Concrete connectors describe themselves with three class
    attributes (:attr:`name`, :attr:`source_id`, :attr:`source_type`) that the
    scheduler and the ingestion pipeline use for reporting and provenance.
    """

    #: Short, stable machine key used to select the connector (e.g. ``"rss"``).
    name: str = "base"
    #: Phase 9.2 ``live_intelligence_sources`` identifier (e.g. ``"rss_feed"``).
    source_id: str = "base"
    #: Phase 9.2 source type governing reliability-tier classification.
    source_type: SourceType = SourceType.MANUAL

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        clock: Clock = utc_now,
        monotonic_clock: MonotonicClock = time.monotonic,
        sleep: SleepFn = time.sleep,
        min_interval_seconds: float = 0.0,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._http_client = http_client
        self._owns_client = http_client is None
        self._clock = clock
        self._timeout = timeout
        self._headers = dict(headers or {})
        self._headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
        self._rate = RateLimiter(min_interval_seconds, clock=monotonic_clock, sleep=sleep)

    @property
    def rate_limiter(self) -> RateLimiter:
        """Expose pacing so callers / tests can inspect the rate-limit stats."""
        return self._rate

    # -- low-level HTTP -----------------------------------------------------

    def _lazy_client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(headers=self._headers, timeout=self._timeout)
            self._owns_client = True
        return self._http_client

    def close(self) -> None:
        """Release the client this connector lazily created, if any.

        Injected clients are owned by the caller and are left untouched.
        """
        if self._owns_client and self._http_client is not None:
            self._http_client.close()
        self._http_client = None
        self._owns_client = False

    def _get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Issue a rate-limited GET, translating failures into typed errors.

        Rate limiting happens *before* the request so a burst can never
        trigger a 429 in the first place (the Phase 9.1 rationale). If the
        source still answers 429, an optional ``Retry-After`` is honoured and
        the call is reported as a :class:`SourceConnectionError`.
        """
        self._rate.acquire()
        client = self._lazy_client()
        try:
            response = client.get(url, params=params, headers=self._headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise SourceConnectionError(f"GET {url} failed: {exc}") from exc

        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                with suppress(ValueError):
                    self._rate.pause(float(retry_after))
            raise SourceConnectionError(f"source rate-limited (429): {url}")
        if response.is_error:
            raise SourceConnectionError(f"GET {url} -> HTTP {response.status_code}")
        return response

    # -- RawItem construction -----------------------------------------------

    def _build_raw_item(
        self,
        *,
        title: str,
        content_text: str,
        published_at: datetime | None,
        url: str | None = None,
        external_id: str | None = None,
        source_id: str | None = None,
    ) -> RawItem | None:
        """Build a :class:`RawItem`, or ``None`` if it cannot be time-consistent.

        The connector's injected clock supplies ``scraped_at`` / ``ingested_at``
        and ``available_at`` defaults to ``published_at`` (we never claim access
        before publication). Items whose ``published_at`` lies in the future
        (clock skew on the source) fail temporal validation and are dropped
        rather than fabricated.
        """
        now = self._clock()
        published = published_at or now
        if published > now:
            return None
        try:
            return RawItem.create(
                source_id=source_id or self.source_id,
                title=title or (url or self.source_id),
                content_text=content_text,
                published_at=published,
                scraped_at=now,
                ingested_at=now,
                url=url,
                external_id=external_id,
            )
        except (ValueError, ValidationError):
            # Impossible temporal footprint or invalid content: skip, do not raise.
            return None

    # -- contract -----------------------------------------------------------

    @abstractmethod
    def fetch(self, *, limit: int | None = None) -> list[RawItem]:
        """Fetch fresh raw items from the source, returning a ``list[RawItem]``.

        ``limit`` caps the number of items returned and is used by dry-runs and
        tests to bound the work a single fetch performs.
        """

    def to_dict(self) -> dict[str, Any]:
        """Describe this connector for the CLI summary."""
        return {
            "name": self.name,
            "source_id": self.source_id,
            "source_type": str(self.source_type),
        }
