"""Phase 22 — provider-neutral shared cache for non-personalized data.

Implements issue #22 (free-first hot path) backend behind a small
provider-neutral interface so the engine can switch between an
in-process dict, an Upstash Redis HTTP backend, or a no-op without
changing call sites.

Design rules
------------

* Interface is the minimum needed for the documented hot path:
  ``get`` / ``set`` / ``delete`` / ``get_or_set`` plus
  ``get_with_metadata`` for stale-while-revalidate semantics.
* ``SharedCache`` (the public facade) layers backend-independent
  features on top of the backend:
  - TTL with optional soft window (stale-while-revalidate).
  - Single-flight deduplication: concurrent identical ``get_or_set``
    calls collapse to one upstream invocation per backend key.
  - Graceful degradation: any backend error falls back to the
    in-process dict and is recorded in stats as ``backend_errors``.
* Keys are namespaced and versioned: every key passes through
  :meth:`SharedCache.build_key` which prepends a configurable namespace
  and a schema version so schema changes cannot collide with stale
  payloads.
* The cache never stores personalized or session-scoped payloads by
  design — call sites must use ``endpoint + params`` keys with no
  per-user identifiers. The facade has no such enforcement because the
  decision belongs to the call site, but the docstring spells it out.
* Stats are exposed in a structured form for the
  ``Phase 0 / observability`` dashboards:
  ``hits / misses / stale / bypass / backend_errors / upstream_saved``.

Backends
--------

* :class:`InMemoryBackend` — process-local ``dict``, used by tests and
  the Vercel warm-Lambda fallback (the engine keeps a small in-process
  cache even when Upstash is configured, to absorb bursty same-instance
  traffic).
* :class:`NullBackend` — every read misses, every write is dropped.
  Used when the operator disables shared caching entirely.
* :class:`UpstashRestBackend` — Upstash Redis free tier via the REST
  API. Backed by ``httpx``. No new SDK dependency.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_NAMESPACE = "fpl"
_SCHEMA_VERSION = "v1"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class SharedCacheStats:
    """Counters for the shared cache facade."""

    hits: int = 0
    misses: int = 0
    stale_hits: int = 0
    bypass: int = 0
    stores: int = 0
    deletes: int = 0
    backend_errors: int = 0
    upstream_saved: int = 0
    single_flight_dedupe: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stale_hits": self.stale_hits,
            "bypass": self.bypass,
            "stores": self.stores,
            "deletes": self.deletes,
            "backend_errors": self.backend_errors,
            "upstream_saved": self.upstream_saved,
            "single_flight_dedupe": self.single_flight_dedupe,
        }


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheEntry:
    """A cached payload with its expiration and metadata."""

    value: Any
    expires_at: float
    stale_until: float | None = None

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def is_stale(self) -> bool:
        return self.stale_until is not None and time.time() >= self.expires_at


class CacheBackend(abc.ABC):
    """Provider-neutral cache backend."""

    @abc.abstractmethod
    def get_entry(self, key: str) -> CacheEntry | None: ...

    @abc.abstractmethod
    def set_entry(self, key: str, entry: CacheEntry) -> None: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...

    def close(self) -> None:
        """Optional cleanup hook (network backends). Default is a no-op."""
        return


class InMemoryBackend(CacheBackend):
    """Process-local ``dict`` backend. Default for tests and CLI."""

    def __init__(self) -> None:
        self._store: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()

    def get_entry(self, key: str) -> CacheEntry | None:
        with self._lock:
            return self._store.get(key)

    def set_entry(self, key: str, entry: CacheEntry) -> None:
        with self._lock:
            self._store[key] = entry

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


class NullBackend(CacheBackend):
    """Every read misses, every write is dropped."""

    def get_entry(self, key: str) -> CacheEntry | None:
        return None

    def set_entry(self, key: str, entry: CacheEntry) -> None:
        return None

    def delete(self, key: str) -> None:
        return None


class UpstashRestBackend(CacheBackend):
    """Upstash Redis REST backend (free tier compatible).

    Uses Upstash's documented HTTP interface. One round-trip per
    command; no SDK dependency. Each command is wrapped so that any
    network/HTTP error raises :class:`UpstashBackendError` which the
    facade records and falls back from.
    """

    def __init__(
        self,
        *,
        rest_url: str,
        rest_token: str,
        timeout: float = 2.0,
        client: Any | None = None,
    ) -> None:
        if not rest_url or not rest_token:
            raise ValueError("rest_url and rest_token are required")
        self._base = rest_url.rstrip("/")
        self._token = rest_token
        self._timeout = float(timeout)
        self._owns_client = client is None
        self._client = client or _build_default_client(self._timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _post(self, path: str, body: list[Any] | None = None) -> dict[str, Any]:
        url = f"{self._base}/{path}"
        try:
            resp = self._client.post(
                url,
                headers=self._headers(),
                json=body or [],
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — record-and-fallback
            raise UpstashBackendError(f"network error: {exc!r}") from exc
        if resp.status_code >= 400:
            raise UpstashBackendError(f"upstash HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise UpstashBackendError(f"upstash non-JSON response: {exc!r}") from exc

    @staticmethod
    def _encode(value: Any) -> str:
        try:
            return json.dumps(value, separators=(",", ":"))
        except (TypeError, ValueError):
            return json.dumps(repr(value))

    def get_entry(self, key: str) -> CacheEntry | None:
        # Upstash GET returns {"result": "<json string or null>"}
        payload = self._post(f"get/{key}")
        result = payload.get("result")
        if result is None:
            return None
        try:
            data = json.loads(result)
        except (TypeError, ValueError) as exc:
            raise UpstashBackendError(f"upstash non-JSON value: {exc!r}") from exc
        if not isinstance(data, dict) or "v" not in data or "e" not in data:
            raise UpstashBackendError("upstash value missing required fields")
        return CacheEntry(
            value=data["v"],
            expires_at=float(data["e"]),
            stale_until=(float(data["s"]) if "s" in data else None),
        )

    def set_entry(self, key: str, entry: CacheEntry) -> None:
        data: dict[str, Any] = {"v": entry.value, "e": entry.expires_at}
        if entry.stale_until is not None:
            data["s"] = entry.stale_until
        encoded = self._encode(data)
        # Upstash SET expects an EX arg in seconds. The stale window is
        # at most equal to the hard TTL, so encode up to the larger of
        # the two.
        hard_ttl = max(entry.expires_at, entry.stale_until or entry.expires_at)
        ttl_seconds = max(int(hard_ttl - time.time()), 1)
        self._post("set", [key, encoded, "EX", ttl_seconds])

    def delete(self, key: str) -> None:
        self._post(f"del/{key}")

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            with contextlib.suppress(Exception):  # best-effort cleanup
                self._client.close()


class UpstashBackendError(RuntimeError):
    """Raised when the Upstash REST backend cannot serve a request."""


def _build_default_client(timeout: float) -> Any:
    import httpx

    return httpx.Client(timeout=timeout)


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------


class SharedCache:
    """TTL + stale-while-revalidate + single-flight cache facade.

    A call site uses:

        cache.get_or_set(
            endpoint="fpl.bootstrap",
            params={"season": "2025/26"},
            ttl_seconds=900,
            soft_ttl_seconds=60,
            fetch=lambda: expensive_call(),
        )

    The cache:
    - reads the backend
    - if hit and fresh -> ``hits``
    - if hit but past TTL but within soft window -> ``stale_hits`` and
      the stale value is returned immediately while a background
      refresh is launched
    - if miss or fully expired -> ``misses`` and ``fetch`` runs, then
      the result is stored
    - concurrent identical ``get_or_set`` calls deduplicate via a
      process-local in-flight map
    - any backend error falls back to a process-local dict and records
      ``backend_errors``
    """

    def __init__(
        self,
        backend: CacheBackend,
        *,
        namespace: str = _DEFAULT_NAMESPACE,
        schema_version: str = _SCHEMA_VERSION,
        fallback: InMemoryBackend | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._backend = backend
        self._namespace = namespace
        self._schema_version = schema_version
        self._fallback = fallback or InMemoryBackend()
        self._clock = clock
        self.stats = SharedCacheStats()
        # Single-flight: backend_key -> in-flight Future-like object
        self._inflight: dict[str, _Inflight] = {}
        self._inflight_lock = threading.Lock()

    # -- key construction ----------------------------------------------------

    def build_key(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        """Build a namespaced, versioned, stable cache key."""
        if not params:
            return f"{self._namespace}:{self._schema_version}:{endpoint}"
        normalised = "&".join(f"{k}={params[k]}" for k in sorted(params, key=lambda s: str(s)))
        return f"{self._namespace}:{self._schema_version}:{endpoint}?{normalised}"

    # -- low-level -----------------------------------------------------------

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any | None:
        """Return the cached value if present and unexpired, else ``None``."""
        key = self.build_key(endpoint, params)
        entry = self._get_via_backend(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            return None
        return entry.value

    def set(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        value: Any = None,
        *,
        ttl_seconds: float,
        soft_ttl_seconds: float = 0.0,
    ) -> None:
        if ttl_seconds <= 0:
            return
        now = self._clock()
        key = self.build_key(endpoint, params)
        entry = CacheEntry(
            value=value,
            expires_at=now + ttl_seconds,
            stale_until=(now + ttl_seconds + soft_ttl_seconds) if soft_ttl_seconds > 0 else None,
        )
        self._set_via_backend(key, entry)
        self.stats.stores += 1

    def delete(self, endpoint: str, params: dict[str, Any] | None = None) -> None:
        key = self.build_key(endpoint, params)
        self._delete_via_backend(key)
        self.stats.deletes += 1

    # -- read-through with single-flight + SWR -------------------------------

    def get_or_set(
        self,
        endpoint: str,
        params: dict[str, Any] | None,
        *,
        fetch: Callable[[], Any],
        ttl_seconds: float,
        soft_ttl_seconds: float = 0.0,
        refresh_async: Callable[[], Any] | None = None,
    ) -> Any:
        """Return a cached value or compute, cache, and return it.

        Single-flight: concurrent identical calls collapse to one
        ``fetch`` invocation. If a stale value is available, return it
        immediately and (optionally) trigger ``refresh_async`` in the
        background to refresh the cache.
        """
        key = self.build_key(endpoint, params)
        entry = self._get_via_backend(key)
        now = self._clock()

        if entry is not None and entry.expires_at > now:
            self.stats.hits += 1
            return entry.value

        if entry is not None and entry.stale_until is not None and entry.stale_until > now:
            self.stats.stale_hits += 1
            self.stats.upstream_saved += 1
            if refresh_async is not None:
                try:
                    refresh_async()
                except Exception:  # noqa: BLE001 — best-effort background refresh
                    logger.debug("background refresh failed for %s", endpoint)
            return entry.value

        # Miss (or fully expired). Single-flight.
        with self._inflight_lock:
            inflight = self._inflight.get(key)
            if inflight is None:
                inflight = _Inflight()
                self._inflight[key] = inflight
                is_leader = True
            else:
                is_leader = False
                self.stats.single_flight_dedupe += 1

        if not is_leader:
            return inflight.wait()

        self.stats.misses += 1
        try:
            value = fetch()
        except Exception:
            with self._inflight_lock:
                self._inflight.pop(key, None)
            inflight.fail()
            raise

        try:
            self.set(
                endpoint,
                params,
                value,
                ttl_seconds=ttl_seconds,
                soft_ttl_seconds=soft_ttl_seconds,
            )
        finally:
            with self._inflight_lock:
                self._inflight.pop(key, None)
            inflight.complete(value)
        return value

    def close(self) -> None:
        with contextlib.suppress(Exception):  # best-effort
            self._backend.close()

    # -- internal: backend dispatch with fallback ----------------------------

    def _get_via_backend(self, key: str) -> CacheEntry | None:
        try:
            entry = self._backend.get_entry(key)
        except Exception as exc:  # noqa: BLE001 — record and fall back
            self.stats.backend_errors += 1
            logger.warning("shared cache backend get failed: %s", exc)
            entry = self._fallback.get_entry(key)
            if entry is not None and self._clock() >= entry.expires_at:
                self._fallback.delete(key)
                return None
        else:
            # Mirror into the in-process fallback so same-process bursts
            # are deduplicated even when the shared backend is healthy.
            if entry is not None and self._fallback.get_entry(key) is None:
                self._fallback.set_entry(key, entry)
        return entry

    def _set_via_backend(self, key: str, entry: CacheEntry) -> None:
        try:
            self._backend.set_entry(key, entry)
        except Exception as exc:  # noqa: BLE001 — record and fall back
            self.stats.backend_errors += 1
            logger.warning("shared cache backend set failed: %s", exc)
        self._fallback.set_entry(key, entry)

    def _delete_via_backend(self, key: str) -> None:
        try:
            self._backend.delete(key)
        except Exception as exc:  # noqa: BLE001
            self.stats.backend_errors += 1
            logger.warning("shared cache backend delete failed: %s", exc)
        self._fallback.delete(key)


@dataclass
class _Inflight:
    """Process-local single-flight holder.

    Minimal value holder with a threading.Event. The cache is
    single-process; cross-process deduplication is not promised.
    """

    event: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    failed: bool = False

    def wait(self) -> Any:
        self.event.wait()
        if self.failed:
            raise RuntimeError("inflight fetch failed")
        return self.value

    def complete(self, value: Any) -> None:
        self.value = value
        self.event.set()

    def fail(self) -> None:
        self.failed = True
        self.event.set()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_shared_cache(
    *,
    backend_name: str = "memory",
    upstash_rest_url: str | None = None,
    upstash_rest_token: str | None = None,
    upstash_timeout: float = 2.0,
    namespace: str = _DEFAULT_NAMESPACE,
    schema_version: str = _SCHEMA_VERSION,
) -> SharedCache:
    """Build a :class:`SharedCache` from settings.

    ``backend_name`` selects:
    * ``"memory"`` -> :class:`InMemoryBackend`
    * ``"null"``   -> :class:`NullBackend`
    * ``"upstash"`` -> :class:`UpstashRestBackend` (requires both
      ``upstash_rest_url`` and ``upstash_rest_token``). Falls back to
      memory if Upstash credentials are missing.
    """
    name = (backend_name or "memory").strip().lower()
    if name == "upstash":
        if upstash_rest_url and upstash_rest_token:
            try:
                backend: CacheBackend = UpstashRestBackend(
                    rest_url=upstash_rest_url,
                    rest_token=upstash_rest_token,
                    timeout=upstash_timeout,
                )
                return SharedCache(
                    backend,
                    namespace=namespace,
                    schema_version=schema_version,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("upstash backend init failed, using memory: %s", exc)
        else:
            logger.info("upstash backend requested but credentials missing; using memory")
        backend = InMemoryBackend()
    elif name == "null":
        backend = NullBackend()
    else:
        backend = InMemoryBackend()
    return SharedCache(
        backend,
        namespace=namespace,
        schema_version=schema_version,
    )


# ---------------------------------------------------------------------------
# Async adapter (optional)
# ---------------------------------------------------------------------------


async def aget_or_set(
    cache: SharedCache,
    endpoint: str,
    params: dict[str, Any] | None,
    *,
    fetch_async: Callable[[], Awaitable[Any]],
    ttl_seconds: float,
    soft_ttl_seconds: float = 0.0,
) -> Any:
    """Async adapter around :meth:`SharedCache.get_or_set`.

    Single-flight dedup still uses the cache's process-local map; the
    sync ``fetch`` wrapper here is provided to keep the in-flight
    ordering simple.
    """

    def _sync_fetch() -> Any:
        return asyncio.get_event_loop().run_until_complete(fetch_async())

    return cache.get_or_set(
        endpoint,
        params,
        fetch=_sync_fetch,
        ttl_seconds=ttl_seconds,
        soft_ttl_seconds=soft_ttl_seconds,
    )
