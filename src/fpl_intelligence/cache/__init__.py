"""Phase 22 — provider-neutral shared cache (issue #22)."""

from fpl_intelligence.cache.settings_adapter import build_shared_cache_from_settings
from fpl_intelligence.cache.shared_cache import (
    CacheBackend,
    CacheEntry,
    InMemoryBackend,
    NullBackend,
    SharedCache,
    SharedCacheStats,
    UpstashBackendError,
    UpstashRestBackend,
    aget_or_set,
    build_shared_cache,
)

__all__ = [
    "CacheBackend",
    "CacheEntry",
    "InMemoryBackend",
    "NullBackend",
    "SharedCache",
    "SharedCacheStats",
    "UpstashBackendError",
    "UpstashRestBackend",
    "aget_or_set",
    "build_shared_cache",
    "build_shared_cache_from_settings",
]
