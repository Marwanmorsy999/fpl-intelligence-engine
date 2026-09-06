"""Phase 22 — bridge between :class:`Settings` and :class:`SharedCache`.

Provides :func:`build_shared_cache_from_settings`, the single
authoritative way the rest of the app constructs a shared cache. Keeps
the rest of the codebase decoupled from the ``pydantic_settings``
Settings model.
"""

from __future__ import annotations

from typing import Any

from fpl_intelligence.cache.shared_cache import SharedCache, build_shared_cache


def build_shared_cache_from_settings(settings: Any) -> SharedCache:
    """Build a :class:`SharedCache` from a :class:`Settings` instance.

    Looks at ``settings.shared_cache_backend`` and the Upstash
    credentials on the settings object. Falls back to the in-memory
    backend when Upstash is selected but credentials are missing
    (preserves behavior of :func:`build_shared_cache`).
    """
    return build_shared_cache(
        backend_name=getattr(settings, "shared_cache_backend", "memory"),
        upstash_rest_url=getattr(settings, "upstash_rest_url", "") or None,
        upstash_rest_token=getattr(settings, "upstash_rest_token", "") or None,
        upstash_timeout=float(getattr(settings, "upstash_timeout_seconds", 2.0)),
        namespace=getattr(settings, "shared_cache_namespace", "fpl"),
    )
