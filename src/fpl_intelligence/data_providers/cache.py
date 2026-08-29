"""Phase 11.1 — Response cache for external football/FPL API calls.

A small, explicit cache so the structured-data connectors never hammer an
external API — in production (rate-limit / politeness) or in tests (where every
response is a mocked JSON fixture and no network call may ever occur).

Design rules
------------
* Cache keys are derived purely from ``endpoint + params`` (params normalised to
  a stable, sorted string) so identical requests collapse to one stored value.
* Two TTL tiers are built in: a 15-minute default for *general* data (e.g.
  bootstrap-static, fixtures) and a 1-minute default for *deadline-sensitive*
  data (e.g. confirmed lineups, team news) that changes right before a gameweek
  lock.
* The clock is injected (default :func:`time.time`) so tests can age entries to
  expiry deterministically without any wall-clock delay.
* An optional ``cache_dir`` persists the cache to a JSON file, used by the
  ``fetch_live_facts.py`` CLI so repeated runs reuse results; in-memory is the
  default and is what the test suite uses.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 15 * 60  # 15 minutes — general data
SENSITIVE_TTL_SECONDS = 60  # 1 minute — deadline-sensitive data


@dataclass
class CacheStats:
    """Counters describing what the cache actually did (for the dry-run report)."""

    hits: int = 0
    misses: int = 0
    stores: int = 0
    expired: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "expired": self.expired,
        }


class ResponseCache:
    """TTL cache keyed by ``endpoint + params``.

    Args:
        default_ttl_seconds: TTL for general data.
        sensitive_ttl_seconds: TTL for deadline-sensitive data.
        clock: Time source returning epoch seconds; injected for tests.
        cache_dir: When set, the cache is persisted to ``<dir>/data_provider_cache.json``.
    """

    def __init__(
        self,
        *,
        default_ttl_seconds: float = DEFAULT_TTL_SECONDS,
        sensitive_ttl_seconds: float = SENSITIVE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        cache_dir: str | Path | None = None,
    ) -> None:
        if default_ttl_seconds < 0 or sensitive_ttl_seconds < 0:
            raise ValueError("TTL values must be non-negative")
        self._default_ttl = float(default_ttl_seconds)
        self._sensitive_ttl = float(sensitive_ttl_seconds)
        self._clock = clock
        self.stats = CacheStats()
        # entry: key -> (stored_at, ttl, value)
        self._store: dict[str, tuple[float, float, Any]] = {}
        self._cache_dir = Path(cache_dir) if cache_dir else None
        if self._cache_dir is not None:
            self._load_persisted()

    # -- key construction ----------------------------------------------------

    @staticmethod
    def make_key(endpoint: str, params: dict[str, Any] | None = None) -> str:
        """Stable cache key from ``endpoint`` and normalised ``params``."""
        if not params:
            return endpoint
        normalised = "&".join(f"{k}={params[k]}" for k in sorted(params, key=lambda s: str(s)))
        return f"{endpoint}?{normalised}"

    # -- read / write --------------------------------------------------------

    def get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        sensitive: bool = False,
    ) -> Any | None:
        """Return the cached value if present and unexpired, else ``None``."""
        key = self.make_key(endpoint, params)
        if key not in self._store:
            self.stats.misses += 1
            return None
        stored_at, ttl, value = self._store[key]
        if self._clock() - stored_at > ttl:
            self.stats.expired += 1
            self.stats.misses += 1
            del self._store[key]
            return None
        self.stats.hits += 1
        return value

    def store(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        value: Any = None,
        *,
        sensitive: bool = False,
        ttl_seconds: float | None = None,
    ) -> None:
        """Cache ``value`` for ``endpoint``/``params``."""
        key = self.make_key(endpoint, params)
        ttl = (
            float(ttl_seconds)
            if ttl_seconds is not None
            else self._sensitive_ttl
            if sensitive
            else self._default_ttl
        )
        if ttl < 0:
            raise ValueError("TTL value must be non-negative")
        self._store[key] = (self._clock(), ttl, value)
        self.stats.stores += 1
        if self._cache_dir is not None:
            self._persist()

    def get_or_fetch(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        sensitive: bool = False,
        fetch_fn: Callable[[], Any] | None = None,
    ) -> Any:
        """Return the cached value, or call ``fetch_fn`` and cache its result."""
        cached = self.get(endpoint, params, sensitive=sensitive)
        if cached is not None:
            return cached
        if fetch_fn is None:
            return None
        value = fetch_fn()
        self.store(endpoint, params, value, sensitive=sensitive)
        return value

    def clear(self) -> None:
        self._store.clear()
        self.stats = CacheStats()

    # -- persistence (CLI only) ---------------------------------------------

    def _persist_path(self) -> Path:
        assert self._cache_dir is not None
        return self._cache_dir / "data_provider_cache.json"

    def _persist(self) -> None:
        if self._cache_dir is None:
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {"stored_at": sa, "ttl": ttl, "value": value}
            for key, (sa, ttl, value) in self._store.items()
        }
        self._persist_path().write_text(json.dumps(payload), encoding="utf-8")

    def _load_persisted(self) -> None:
        path = self._persist_path()
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        for key, entry in payload.items():
            try:
                self._store[key] = (
                    float(entry["stored_at"]),
                    float(entry["ttl"]),
                    entry["value"],
                )
            except (KeyError, TypeError, ValueError):
                continue
