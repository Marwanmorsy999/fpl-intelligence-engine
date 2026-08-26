"""Phase 4.3 — edge caching policy for Cloudflare / shared CDNs.

Cloudflare's free tier honours `Cache-Control` response headers (no paid
"Cache Everything" rules needed). This module centralises *every* cache policy
so the whole API follows one auditable contract instead of hand-scattered
header tweaks:

* public bootstrap-derived / prediction payloads  -> CDN-cacheable (1h).
* news RSS (cheap, volatile)                     -> short CDN TTL (15 min).
* `/api/v1/health` (warmth + observability)     -> short public TTL (5 min).
* squad / user-specific payloads                -> NEVER cached at the edge.

The policy is applied centrally by :class:`EdgeCachePolicyMiddleware` for
read-only (``GET``/``HEAD``) requests only — writes (POST/…) are never tagged.
Applying a header centrally allows the CDN to cache without any rule
configuration, and a plain unit-testable function keeps the mapping explicit.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

#: Health endpoint — short public TTL: cheap to recompute and CDN-cacheable.
HEALTH_POLICY = "public, max-age=300"

#: BBC RSS feeds are cheap to fetch but change often — short CDN cache with a
#: generous stale-while-revalidate window so a cold-origin still reads fresh.
NEWS_RSS_POLICY = "public, s-maxage=900, stale-while-revalidate=3600"

#: Bootstrap-derived / prediction payloads — expensive to compute, cheap to
#: serve. CDN-cache for an hour and serve stale for up to a day while the
#: origin refreshes. Compatible with Cloudflare's free honouring of s-maxage.
BOOTSTRAP_POLICY = "public, s-maxage=3600, stale-while-revalidate=86400"

#: Personal / per-session endpoints — never cache at the edge.
PRIVATE_POLICY = "private, no-store"

#: Any path under these prefixes is user/session specific and must never be
#: cached at the edge (session_id keyed, personal squad/league/decision data).
_PRIVATE_PREFIXES: tuple[str, ...] = (
    "/api/v1/squad",
    "/api/v1/league",
    "/api/v1/decisions",
    "/api/v1/news/radar",
    "/api/v1/dashboard",
)

#: Paths that own the short RSS news policy (both the bare and versioned paths
#: may be served — news router is mounted at /news and /api/v1/news).
_NEWS_RSS_PATHS: frozenset[str] = frozenset(
    {"/news/bbc-rss", "/api/v1/news/bbc-rss"}
)


def cache_control_for(method: str, path: str) -> str | None:
    """Return the ``Cache-Control`` value for a request, or ``None`` if none.

    ``None`` means "leave the response header alone" (e.g. non-GET requests or
    non-API static assets). Only readable requests may be cached.
    """
    if method not in {"GET", "HEAD"}:
        return None
    if path == "/api/v1/health":
        return HEALTH_POLICY
    if path in _NEWS_RSS_PATHS:
        return NEWS_RSS_POLICY
    for prefix in _PRIVATE_PREFIXES:
        if path.startswith(prefix):
            return PRIVATE_POLICY
    if path.startswith("/api/v1/"):
        return BOOTSTRAP_POLICY
    return None


class EdgeCachePolicyMiddleware(BaseHTTPMiddleware):
    """Set ``Cache-Control`` on readable responses per the shared contract.

    A response header set by a route already (e.g. ``no-store`` on a session
    endpoint) is never overwritten — the specific (more conservative) route
    wins. Otherwise the central policy for that path is applied.
    """

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        policy = cache_control_for(request.method, request.url.path)
        if policy is not None and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = policy
        return response