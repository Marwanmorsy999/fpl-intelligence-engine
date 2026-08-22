"""Phase 18.0 — Egress mask chain for the official FPL API.

Vercel's shared egress IPs are intermittently 403/429'd by the official FPL
API, which previously surfaced as hard 500s on ``/api/v1/squad/from-fpl``. This
module defines an ordered chain of fetch strategies; each URL is attempted
through every strategy (short timeout each) until one returns a payload that
passes a caller-supplied shape validator.

Strategy order
--------------
1. ``direct``       — existing browser-User-Agent GET.
2. ``allorigins``   — ``https://api.allorigins.win/raw?url=<encoded>``.
3. ``corsproxy``    — ``https://corsproxy.io/?url=<encoded>``.
4. ``env_proxy``    — ``$FPL_PROXY_URL?url=<encoded>`` (user's Apps Script).

No strategy ever transmits auth or secrets: only public FPL URLs are passed to
any mask, and the mask URL itself is taken from env (``FPL_PROXY_URL``), never
from request input. The winning strategy is logged per call and surfaced in the
sync-status line so the user knows exactly which path reached FPL.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

#: Browser-like headers for the direct strategy — the official FPL API rejects
#: requests that look like bots.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

#: Per-strategy network timeout (seconds). Short so a blocked mask fails fast
#: and we fall through to the next one.
STRATEGY_TIMEOUT = 4.0

#: TTL for the in-process response cache.
DEFAULT_CACHE_TTL_SECONDS = 60


class FplEgressError(RuntimeError):
    """Base for egress-chain failures."""


class FplEgressExhaustedError(FplEgressError):
    """Every strategy in the chain failed. Carries the per-strategy diagnostics."""

    def __init__(self, path: str, attempts: list[tuple[str, str]]) -> None:
        self.path = path
        self.attempts = attempts
        summary = "; ".join(f"{name}: {err}" for name, err in attempts)
        super().__init__(f"All egress strategies failed for {path} — {summary}")


def validate_entry_payload(data: Any) -> None:
    """An entry payload must be a JSON object containing an ``id``."""
    if not isinstance(data, dict) or "id" not in data:
        raise ValueError(f"entry payload missing 'id' (got {type(data).__name__})")


def validate_picks_payload(data: Any) -> None:
    """A picks payload must be a JSON object containing a ``picks`` list."""
    if not isinstance(data, dict) or "picks" not in data:
        raise ValueError(f"picks payload missing 'picks' (got {type(data).__name__})")


def validate_bootstrap_payload(data: Any) -> None:
    """A bootstrap payload must be a JSON object containing an ``elements`` list."""
    if not isinstance(data, dict) or "elements" not in data:
        raise ValueError(f"bootstrap payload missing 'elements' (got {type(data).__name__})")


class FplEgressChain:
    """Ordered chain of egress strategies for fetching FPL JSON.

    Args:
        base_url: FPL base URL (e.g. ``https://fantasy.premierleague.com``).
        timeout: Per-strategy network timeout in seconds.
        cache_ttl: How long a successful response is reused (seconds).
        monotonic_clock: Injectable for tests.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = STRATEGY_TIMEOUT,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout)
        self._cache_ttl = float(cache_ttl)
        self._now = monotonic_clock or time.monotonic
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._winning_strategy: str | None = None

    # -- public API ----------------------------------------------------------

    @property
    def winning_strategy(self) -> str | None:
        """Name of the strategy that won the most recent ``fetch`` (or ``None``)."""
        return self._winning_strategy

    def cache_stats(self) -> dict[str, int]:
        return {"size": len(self._cache), "ttl": int(self._cache_ttl)}

    async def fetch(
        self,
        path: str,
        *,
        validator: Callable[[Any], None] | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Fetch ``base_url + path`` through the chain.

        Strategies are tried in order. The first response that passes the
        ``validator`` (if any) is cached and returned. When every strategy
        fails, :class:`FplEgressExhaustedError` lists each failure so callers
        can show an honest ``blocked`` message instead of a bare 500.
        """
        full_path = path if path.startswith("/") else f"/{path}"
        cache_key = full_path

        if use_cache:
            cached = self._cache.get(cache_key)
            if cached:
                ts, data = cached
                if self._now() - ts < self._cache_ttl:
                    logger.debug("fpl_egress: cache hit %s", full_path)
                    return data

        url = f"{self._base_url}{full_path}"
        attempts: list[tuple[str, str]] = []

        for name, fn in self._strategies():
            try:
                data = await fn(url)
            except Exception as exc:  # noqa: BLE001 — record and fall through
                attempts.append((name, f"{type(exc).__name__}: {exc}"))
                logger.debug("fpl_egress: %s -> %s failed: %s", full_path, name, exc)
                continue

            if validator is not None:
                try:
                    validator(data)
                except Exception as exc:  # noqa: BLE001 — shape rejected
                    attempts.append((name, f"shape rejected: {exc}"))
                    logger.debug("fpl_egress: %s -> %s shape rejected: %s", full_path, name, exc)
                    continue

            self._winning_strategy = name
            self._cache[cache_key] = (self._now(), data)
            logger.info("fpl_egress: %s -> strategy=%s OK", full_path, name)
            return data

        raise FplEgressExhaustedError(full_path, attempts)

    async def fetch_with_client(
        self,
        path: str,
        client: httpx.AsyncClient,
        *,
        validator: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        """Fetch using an existing client for the direct strategy.

        When the caller already owns an ``httpx.AsyncClient`` (e.g. an
        importer that reuses one socket), the direct strategy uses it instead
        of spinning up a fresh client. Mask strategies still use their own
        clients (they hit different hosts).
        """
        full_path = path if path.startswith("/") else f"/{path}"
        url = f"{self._base_url}{full_path}"
        attempts: list[tuple[str, str]] = []

        async def _direct_with(client: httpx.AsyncClient, target_url: str) -> Any:
            r = await client.get(target_url)
            r.raise_for_status()
            return r.json()

        strategies: list[tuple[str, Callable[[], Any]]] = [
            ("direct", lambda: _direct_with(client, url)),
        ]
        for name, fn in self._mask_strategies():
            strategies.append((name, fn))

        for name, fn in strategies:
            try:
                data = await fn()
            except Exception as exc:  # noqa: BLE001
                attempts.append((name, f"{type(exc).__name__}: {exc}"))
                continue

            if validator is not None:
                try:
                    validator(data)
                except Exception as exc:  # noqa: BLE001
                    attempts.append((name, f"shape rejected: {exc}"))
                    continue

            self._winning_strategy = name
            logger.info("fpl_egress: %s -> strategy=%s OK", full_path, name)
            return data

        raise FplEgressExhaustedError(full_path, attempts)

    # -- strategies ---------------------------------------------------------

    def _strategies(self) -> list[tuple[str, Callable[[str], Any]]]:
        """Direct strategy + every mask strategy, in priority order."""
        return [("direct", self._direct), *self._mask_strategies()]

    def _mask_strategies(self) -> list[tuple[str, Callable[[str], Any]]]:
        return [
            ("allorigins", self._allorigins),
            ("corsproxy", self._corsproxy),
            ("env_proxy", self._env_proxy),
        ]

    async def _direct(self, url: str) -> Any:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers=_BROWSER_HEADERS,
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()

    async def _allorigins(self, url: str) -> Any:
        return await self._proxy_get(f"https://api.allorigins.win/raw?url={_enc(url)}")

    async def _corsproxy(self, url: str) -> Any:
        return await self._proxy_get(f"https://corsproxy.io/?url={_enc(url)}")

    async def _env_proxy(self, url: str) -> Any:
        base = os.getenv("FPL_PROXY_URL", "").strip()
        if not base:
            raise FplEgressError("FPL_PROXY_URL not set")
        # Strip any trailing ?url= so we control the query param ourselves.
        base = base.split("?url=")[0].rstrip("?&")
        return await self._proxy_get(f"{base}?url={_enc(url)}")

    async def _proxy_get(self, proxy_url: str) -> Any:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
        ) as client:
            r = await client.get(proxy_url)
            r.raise_for_status()
            data = r.json()
            # Some masks wrap the upstream payload; unwrap common shapes.
            if isinstance(data, dict) and isinstance(data.get("contents"), str):
                # allorigins returns {"contents": "<json string>", ...}
                try:
                    return json.loads(data["contents"])
                except (ValueError, TypeError) as exc:
                    raise FplEgressError(f"mask contents not JSON: {exc}") from exc
            return data


def _enc(url: str) -> str:
    """URL-encode for safe embedding in a mask query string."""
    return quote(url, safe="")
