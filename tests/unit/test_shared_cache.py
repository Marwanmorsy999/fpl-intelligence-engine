"""Tests for the provider-neutral shared cache (Phase 22, issue #22).

Covers:
- Backend ABC contract (in-memory, null, Upstash).
- TTL + soft-window expiry semantics.
- Single-flight deduplication.
- Backend-error fallback to the in-process dict.
- Stats counters (hits / misses / stale_hits / backend_errors / upstream_saved
  / single_flight_dedupe).
- Upstash REST wire format via a recording httpx mock.
- Key namespacing and schema version.
- No personalization leaks: keys never include per-user identifiers
  unless the call site adds them.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from fpl_intelligence.cache.shared_cache import (
    CacheEntry,
    InMemoryBackend,
    NullBackend,
    SharedCache,
    UpstashBackendError,
    UpstashRestBackend,
    build_shared_cache,
)


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


# ---------------------------------------------------------------------------
# Backend contract
# ---------------------------------------------------------------------------


def test_in_memory_backend_round_trip() -> None:
    backend = InMemoryBackend()
    entry = CacheEntry(value={"a": 1}, expires_at=time.time() + 60)
    backend.set_entry("k", entry)
    got = backend.get_entry("k")
    assert got is entry
    backend.delete("k")
    assert backend.get_entry("k") is None


def test_null_backend_is_passthrough() -> None:
    backend = NullBackend()
    backend.set_entry("k", CacheEntry(value=1, expires_at=time.time() + 60))
    assert backend.get_entry("k") is None
    backend.delete("k")  # no error


# ---------------------------------------------------------------------------
# Facade: key + TTL + SWR
# ---------------------------------------------------------------------------


def test_build_key_namespaces_and_versions() -> None:
    cache = SharedCache(InMemoryBackend(), namespace="fpl", schema_version="v1")
    k1 = cache.build_key("bootstrap", {"season": "2025/26"})
    k2 = cache.build_key("bootstrap", {"season": "2025/26"})
    k3 = cache.build_key("bootstrap", {"season": "2024/25"})
    assert k1 == k2
    assert k1 != k3
    assert k1.startswith("fpl:v1:bootstrap")


def test_set_respects_ttl() -> None:
    clock = _Clock()
    cache = SharedCache(InMemoryBackend(), clock=clock)
    cache.set("ep", {"x": 1}, value=42, ttl_seconds=10)
    assert cache.get("ep", {"x": 1}) == 42
    clock.advance(11)
    assert cache.get("ep", {"x": 1}) is None


def test_get_or_set_returns_cached_value_on_hit() -> None:
    cache = SharedCache(InMemoryBackend())
    calls = {"n": 0}

    def fetch() -> int:
        calls["n"] += 1
        return calls["n"]

    v1 = cache.get_or_set("ep", {"x": 1}, fetch=fetch, ttl_seconds=60, soft_ttl_seconds=0)
    v2 = cache.get_or_set("ep", {"x": 1}, fetch=fetch, ttl_seconds=60, soft_ttl_seconds=0)
    assert v1 == v2 == 1
    assert calls["n"] == 1
    assert cache.stats.hits == 1
    assert cache.stats.misses == 1


def test_get_or_set_uses_stale_value_and_triggers_background_refresh() -> None:
    clock = _Clock()
    cache = SharedCache(InMemoryBackend(), clock=clock)
    fetched = {"n": 0}
    refreshed = {"n": 0}

    def fetch() -> int:
        fetched["n"] += 1
        return fetched["n"]

    def refresh() -> None:
        refreshed["n"] += 1

    v1 = cache.get_or_set(
        "ep",
        {"x": 1},
        fetch=fetch,
        ttl_seconds=10,
        soft_ttl_seconds=20,
        refresh_async=refresh,
    )
    assert v1 == 1
    # Move past hard TTL but inside soft window
    clock.advance(15)
    v2 = cache.get_or_set(
        "ep",
        {"x": 1},
        fetch=fetch,
        ttl_seconds=10,
        soft_ttl_seconds=20,
        refresh_async=refresh,
    )
    assert v2 == 1
    assert refreshed["n"] == 1
    assert fetched["n"] == 1
    assert cache.stats.stale_hits == 1
    assert cache.stats.upstream_saved == 1


def test_get_or_set_single_flight_dedup() -> None:
    cache = SharedCache(InMemoryBackend())
    started = threading.Event()
    release = threading.Event()
    fetched = {"n": 0}

    def slow_fetch() -> int:
        fetched["n"] += 1
        started.set()
        release.wait(timeout=2.0)
        return fetched["n"]

    results: list[int] = []

    def worker() -> None:
        results.append(
            cache.get_or_set(
                "ep",
                {"x": 1},
                fetch=slow_fetch,
                ttl_seconds=60,
                soft_ttl_seconds=0,
            )
        )

    t1 = threading.Thread(target=worker)
    t1.start()
    started.wait(timeout=1.0)
    t2 = threading.Thread(target=worker)
    t2.start()
    # Brief pause so t2 enters get_or_set and registers as dedupe
    time.sleep(0.05)
    release.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert fetched["n"] == 1
    assert results == [1, 1]
    assert cache.stats.single_flight_dedupe == 1


def test_get_or_set_propagates_fetch_error_and_releases_inflight() -> None:
    cache = SharedCache(InMemoryBackend())

    def boom() -> None:
        raise RuntimeError("upstream")

    with pytest.raises(RuntimeError, match="upstream"):
        cache.get_or_set("ep", {"x": 1}, fetch=boom, ttl_seconds=60)
    # Subsequent call must run again (in-flight cleared)
    fallback_called = {"n": 0}

    def fetch() -> int:
        fallback_called["n"] += 1
        return 99

    assert cache.get_or_set("ep", {"x": 1}, fetch=fetch, ttl_seconds=60) == 99
    assert fallback_called["n"] == 1


# ---------------------------------------------------------------------------
# Backend-error fallback
# ---------------------------------------------------------------------------


def _always_failing_backend() -> Any:
    backend = MagicMock()
    backend.get_entry.side_effect = UpstashBackendError("boom")
    backend.set_entry.side_effect = UpstashBackendError("boom")
    backend.delete.side_effect = UpstashBackendError("boom")
    return backend


def test_backend_error_falls_back_to_in_process_store() -> None:
    backend = _always_failing_backend()
    cache = SharedCache(backend)
    cache.set("ep", {"x": 1}, value=42, ttl_seconds=60)
    # Backend failed; in-process fallback served the read
    assert cache.get("ep", {"x": 1}) == 42
    assert cache.stats.backend_errors >= 2  # set + (any) get that uses backend
    assert cache.stats.stores == 1


def test_backend_get_failure_returns_none_and_falls_back_to_miss() -> None:
    backend = MagicMock()
    backend.get_entry.side_effect = UpstashBackendError("boom")
    cache = SharedCache(backend)
    # No prior set; fall-through after backend error leaves us with no value
    assert cache.get("ep", {"x": 1}) is None


# ---------------------------------------------------------------------------
# Upstash REST wire format
# ---------------------------------------------------------------------------


def test_upstash_backend_serializes_entry_and_uses_ex() -> None:
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"result": "OK"}
    client.post.return_value = response
    backend = UpstashRestBackend(
        rest_url="https://example.upstash.io",
        rest_token="secret",
        client=client,
    )
    entry = CacheEntry(value={"a": 1}, expires_at=time.time() + 60)
    backend.set_entry("k", entry)

    args, kwargs = client.post.call_args
    assert args[0] == "https://example.upstash.io/set"
    payload_args = kwargs["json"]
    assert payload_args[0] == "k"
    assert payload_args[2] == "EX"
    assert isinstance(payload_args[3], int) and payload_args[3] >= 1
    encoded = payload_args[1]
    import json as _json

    decoded = _json.loads(encoded)
    assert decoded["v"] == {"a": 1}
    assert "e" in decoded


def test_upstash_backend_round_trip_value() -> None:
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    encoded_value = '{"v": 42, "e": ' + str(time.time() + 60) + "}"
    response.json.return_value = {"result": encoded_value}
    client.post.return_value = response
    backend = UpstashRestBackend(
        rest_url="https://example.upstash.io",
        rest_token="secret",
        client=client,
    )
    entry = backend.get_entry("k")
    assert entry is not None
    assert entry.value == 42


def test_upstash_backend_raises_on_http_error() -> None:
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 500
    resp.text = "kaboom"
    client.post.return_value = resp
    backend = UpstashRestBackend(
        rest_url="https://example.upstash.io",
        rest_token="secret",
        client=client,
    )
    with pytest.raises(UpstashBackendError):
        backend.get_entry("k")


# ---------------------------------------------------------------------------
# Factory + settings bridge
# ---------------------------------------------------------------------------


def test_build_shared_cache_memory_default() -> None:
    cache = build_shared_cache()
    assert isinstance(cache._backend, InMemoryBackend)


def test_build_shared_cache_null() -> None:
    cache = build_shared_cache(backend_name="null")
    assert isinstance(cache._backend, NullBackend)


def test_build_shared_cache_upstash_without_credentials_falls_back() -> None:
    cache = build_shared_cache(
        backend_name="upstash",
        upstash_rest_url="",
        upstash_rest_token="",
    )
    assert isinstance(cache._backend, InMemoryBackend)


def test_build_shared_cache_upstash_with_credentials() -> None:
    cache = build_shared_cache(
        backend_name="upstash",
        upstash_rest_url="https://example.upstash.io",
        upstash_rest_token="tkn",
    )
    assert isinstance(cache._backend, UpstashRestBackend)


def test_build_shared_cache_from_settings_adapter() -> None:
    settings = MagicMock()
    settings.shared_cache_backend = "memory"
    settings.upstash_rest_url = ""
    settings.upstash_rest_token = ""
    settings.upstash_timeout_seconds = 1.0
    settings.shared_cache_namespace = "fpl"
    from fpl_intelligence.cache.settings_adapter import (
        build_shared_cache_from_settings,
    )

    cache = build_shared_cache_from_settings(settings)
    assert isinstance(cache._backend, InMemoryBackend)


# ---------------------------------------------------------------------------
# No personalization leak (documentation guard)
# ---------------------------------------------------------------------------


def test_keys_are_not_per_user_by_default() -> None:
    """Call sites must not put user identifiers in the cache key.

    This test does not enforce (that belongs at the call site) but it
    documents the invariant by example.
    """
    cache = SharedCache(InMemoryBackend())
    key = cache.build_key("bootstrap", {"season": "2025/26"})
    assert "entry_id" not in key
    assert "session_id" not in key
    assert "user_id" not in key


# ---------------------------------------------------------------------------
# Stats shape
# ---------------------------------------------------------------------------


def test_stats_dict_has_all_expected_fields() -> None:
    cache = SharedCache(InMemoryBackend())

    def fetch() -> int:
        return 1

    cache.get_or_set("ep", {"x": 1}, fetch=fetch, ttl_seconds=60)
    cache.get_or_set("ep", {"x": 1}, fetch=fetch, ttl_seconds=60)  # hit
    cache.get_or_set("ep", {"x": 2}, fetch=fetch, ttl_seconds=60)  # miss
    cache.delete("ep", {"x": 1})
    d = cache.stats.to_dict()
    assert set(d) == {
        "hits",
        "misses",
        "stale_hits",
        "bypass",
        "stores",
        "deletes",
        "backend_errors",
        "upstream_saved",
        "single_flight_dedupe",
    }
    assert d["stores"] >= 2
    assert d["hits"] >= 1
    assert d["misses"] >= 2
    assert d["deletes"] >= 1
