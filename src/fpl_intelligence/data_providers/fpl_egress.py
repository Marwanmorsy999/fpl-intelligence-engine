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
4. ``codetabs``     — ``https://api.codetabs.com/v1/proxy?quest=<encoded>``.
5. ``env_proxy``    — ``$FPL_PROXY_URL?url=<encoded>`` (user's Apps Script or
                     the free Cloudflare Worker in ``scripts/``).

``fetch()`` returns parsed JSON; ``fetch_text()`` (pass 2) runs the SAME chain
in raw-body mode for targets whose payload is not JSON (Understat league HTML
pages were previously discarded as JSONDecodeError even on a healthy 200).

No strategy ever transmits auth or secrets: only public FPL URLs are passed to
any mask, and the mask URL itself is taken from env (``FPL_PROXY_URL``), never
from request input. The winning strategy is logged per call and surfaced in the
sync-status line so the user knows exactly which path reached FPL.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Phase 20.4 — per-mask health ledger.
#
# Every direct/mask attempt through the chain records its outcome here so the
# Sources page can show an honest "last status per strategy" table. The
# registry is process-local: Vercel instances are ephemeral, so rows describe
# what THIS instance saw most recently, never a fabricated history.
# --------------------------------------------------------------------------- #

_MASK_HEALTH_LOCK = threading.Lock()
_MASK_HEALTH: dict[str, dict[str, Any]] = {}

_HEALTH_FIELDS = (
    "last_status",
    "last_at",
    "last_error",
    "success_count",
    "fail_count",
)


def record_strategy_result(name: str, *, ok: bool, detail: str = "") -> None:
    """Record one attempt outcome for a strategy name (thread-safe)."""
    now_iso = datetime.now(UTC).isoformat()
    with _MASK_HEALTH_LOCK:
        row = _MASK_HEALTH.setdefault(
            name,
            {
                "last_status": "",
                "last_at": None,
                "last_error": "",
                "success_count": 0,
                "fail_count": 0,
            },
        )
        if ok:
            row["success_count"] += 1
            row["last_status"] = "ok"
            row["last_error"] = ""
        else:
            row["fail_count"] += 1
            # A later success clears the error; a failure overwrites it.
            row["last_status"] = "fail"
            row["last_error"] = detail[:300]
        row["last_at"] = now_iso


def reset_mask_health() -> None:
    """Clear the ledger (tests only)."""
    with _MASK_HEALTH_LOCK:
        _MASK_HEALTH.clear()


def mask_health_payload() -> list[dict[str, Any]]:
    """Per-strategy health rows ordered by chain priority."""
    order = ["direct", "allorigins", "corsproxy", "codetabs", "env_proxy"]
    with _MASK_HEALTH_LOCK:
        snapshot = {k: dict(v) for k, v in _MASK_HEALTH.items()}
    known = [name for name in order if name in snapshot]
    extra = sorted(set(snapshot) - set(order))
    rows = []
    for name in known + extra:
        row = snapshot[name]
        rows.append(
            {
                "strategy": name,
                **{f: row.get(f) for f in _HEALTH_FIELDS},
            }
        )
    return rows

#: Browser-like headers for the direct strategy — the official FPL API rejects
#: requests that look like bots.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

#: Same browser identity, HTML-tolerant Accept for the text-mode direct fetch.
_BROWSER_HEADERS_TEXT = {**_BROWSER_HEADERS, "Accept": "text/html,*/*"}

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
                record_strategy_result(
                    name, ok=False, detail=f"{type(exc).__name__}: {exc}"
                )
                logger.debug("fpl_egress: %s -> %s failed: %s", full_path, name, exc)
                continue

            if validator is not None:
                try:
                    validator(data)
                except Exception as exc:  # noqa: BLE001 — shape rejected
                    attempts.append((name, f"shape rejected: {exc}"))
                    record_strategy_result(name, ok=False, detail=f"shape rejected: {exc}")
                    logger.debug("fpl_egress: %s -> %s shape rejected: %s", full_path, name, exc)
                    continue

            self._winning_strategy = name
            record_strategy_result(name, ok=True)
            self._cache[cache_key] = (self._now(), data)
            logger.info("fpl_egress: %s -> strategy=%s OK", full_path, name)
            return data

        raise FplEgressExhaustedError(full_path, attempts)

    async def fetch_text(
        self,
        path: str,
        *,
        use_cache: bool = True,
    ) -> str:
        """Fetch ``base_url + path`` as RAW TEXT through the same chain.

        The JSON chain (``r.json()``) rejects any non-JSON body, so a healthy
        200 HTML page (Understat league pages) was discarded as
        JSONDecodeError on every strategy — the Sources page then stuck on a
        permanent stale "page reachable but no playersData block" status.
        Text mode keeps the SAME strategy order and per-strategy health
        accounting but returns the raw body, so a 200 HTML response counts as
        reachable and the caller parses whatever it needs.

        allorigins may answer with a ``{"contents": "<raw body>"}`` wrapper;
        that is unwrapped before returning. Successful results are cached
        under ``text:<path>`` so they never collide with JSON-mode entries.
        """
        full_path = path if path.startswith("/") else f"/{path}"
        cache_key = f"text:{full_path}"

        if use_cache:
            cached = self._cache.get(cache_key)
            if cached:
                ts, data = cached
                if self._now() - ts < self._cache_ttl:
                    logger.debug("fpl_egress: text cache hit %s", full_path)
                    return data

        url = f"{self._base_url}{full_path}"

        # Plain async closures for the mask strategies (text variants of the
        # JSON chain) — the direct strategy uses the shared _direct_text.
        async def _get_text(proxy_url: str) -> str:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=True
            ) as client:
                r = await client.get(proxy_url)
                r.raise_for_status()
                return r.text

        async def _allorigins_text(target: str) -> str:
            body = await _get_text(f"https://api.allorigins.win/raw?url={_enc(target)}")
            # allorigins may re-encode the upstream body as {"contents": ...}.
            if body.lstrip()[:1] == "{":
                try:
                    parsed = json.loads(body)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict) and isinstance(parsed.get("contents"), str):
                    return parsed["contents"]
            return body

        async def _corsproxy_text(target: str) -> str:
            return await _get_text(f"https://corsproxy.io/?url={_enc(target)}")

        async def _codetabs_text(target: str) -> str:
            return await _get_text(f"https://api.codetabs.com/v1/proxy?quest={_enc(target)}")

        async def _env_proxy_text(target: str) -> str:
            base = os.getenv("FPL_PROXY_URL", "").strip()
            if not base:
                raise FplEgressError("FPL_PROXY_URL not set")
            # Strip any trailing ?url= so we control the query param ourselves.
            base = base.split("?url=")[0].rstrip("?&")
            return await _get_text(f"{base}?url={_enc(target)}")

        strategies: list[tuple[str, Callable[[str], Any]]] = [
            ("direct", self._direct_text),
            ("allorigins", _allorigins_text),
            ("corsproxy", _corsproxy_text),
            ("codetabs", _codetabs_text),
            ("env_proxy", _env_proxy_text),
        ]

        attempts: list[tuple[str, str]] = []
        for name, fn in strategies:
            try:
                text = await fn(url)
            except Exception as exc:  # noqa: BLE001 — record and fall through
                attempts.append((name, f"{type(exc).__name__}: {exc}"))
                record_strategy_result(name, ok=False, detail=f"{type(exc).__name__}: {exc}")
                logger.debug("fpl_egress: %s -> %s (text) failed: %s", full_path, name, exc)
                continue

            if not isinstance(text, str) or not text.strip():
                attempts.append((name, "shape rejected: empty or non-text body"))
                record_strategy_result(name, ok=False, detail="empty or non-text body")
                continue

            self._winning_strategy = name
            record_strategy_result(name, ok=True)
            self._cache[cache_key] = (self._now(), text)
            logger.info("fpl_egress: %s -> strategy=%s OK (text)", full_path, name)
            return text

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
                record_strategy_result(
                    name, ok=False, detail=f"{type(exc).__name__}: {exc}"
                )
                continue

            if validator is not None:
                try:
                    validator(data)
                except Exception as exc:  # noqa: BLE001
                    attempts.append((name, f"shape rejected: {exc}"))
                    record_strategy_result(name, ok=False, detail=f"shape rejected: {exc}")
                    continue

            self._winning_strategy = name
            record_strategy_result(name, ok=True)
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
            ("codetabs", self._codetabs),
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

    async def _direct_text(self, url: str) -> str:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers=_BROWSER_HEADERS_TEXT,
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text

    async def _allorigins(self, url: str) -> Any:
        return await self._proxy_get(f"https://api.allorigins.win/raw?url={_enc(url)}")

    async def _corsproxy(self, url: str) -> Any:
        return await self._proxy_get(f"https://corsproxy.io/?url={_enc(url)}")

    async def _codetabs(self, url: str) -> Any:
        return await self._proxy_get(f"https://api.codetabs.com/v1/proxy?quest={_enc(url)}")

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
