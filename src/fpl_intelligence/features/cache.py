"""Feature cache for the FPL Intelligence Engine.

Provides thread-safe caching of computed feature snapshots. Cache keys
include feature_version, entity_id, cutoff_time, and data_version to
ensure that cached values are never reused across different cutoffs
or feature versions.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any


class FeatureCache:
    """Thread-safe cache for computed feature snapshots.

    Cache keys are composed of:
        - feature_name
        - feature_version
        - entity_id
        - cutoff_time (ISO format)
        - data_version (optional, for cache invalidation)

    This ensures that cached values are never reused across different
    cutoffs or feature versions, preventing look-ahead leakage.
    """

    def __init__(self, max_size: int = 10_000) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    def _make_key(
        self,
        feature_name: str,
        feature_version: str,
        entity_id: int,
        cutoff_time: datetime,
        data_version: str | None = None,
    ) -> str:
        """Build a deterministic cache key."""
        key_parts = [
            feature_name,
            feature_version,
            str(entity_id),
            cutoff_time.isoformat(),
            data_version or "default",
        ]
        raw = "|".join(key_parts)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(
        self,
        feature_name: str,
        feature_version: str,
        entity_id: int,
        cutoff_time: datetime,
        data_version: str | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a cached feature result.

        Returns None if not cached or if the cache entry is stale.
        """
        key = self._make_key(
            feature_name, feature_version, entity_id, cutoff_time, data_version
        )
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            self._hits += 1
            return entry.copy()

    def set(
        self,
        feature_name: str,
        feature_version: str,
        entity_id: int,
        cutoff_time: datetime,
        value: dict[str, Any],
        data_version: str | None = None,
    ) -> None:
        """Store a feature result in the cache."""
        key = self._make_key(
            feature_name, feature_version, entity_id, cutoff_time, data_version
        )
        with self._lock:
            if len(self._cache) >= self._max_size:
                # Evict oldest entry (simple FIFO)
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            self._cache[key] = value.copy()

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def invalidate(
        self,
        feature_name: str | None = None,
        feature_version: str | None = None,
        entity_id: int | None = None,
    ) -> int:
        """Invalidate cache entries matching the given criteria.

        Returns the number of entries invalidated.
        """
        with self._lock:
            if feature_name is None and feature_version is None and entity_id is None:
                count = len(self._cache)
                self._cache.clear()
                return count

            keys_to_remove = []
            for key, entry in self._cache.items():
                # We can't easily match partial keys, so we check the entry
                # for matching feature_name/version/entity_id
                entry_name = entry.get("_feature_name", "")
                entry_version = entry.get("_feature_version", "")
                entry_entity = entry.get("_entity_id", None)

                if feature_name and entry_name != feature_name:
                    continue
                if feature_version and entry_version != feature_version:
                    continue
                if entity_id is not None and entry_entity != entity_id:
                    continue
                keys_to_remove.append(key)

            for key in keys_to_remove:
                del self._cache[key]
            return len(keys_to_remove)

    @property
    def stats(self) -> dict[str, int]:
        """Return cache statistics."""
        with self._lock:
            return {
                "size": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0.0,
            }
