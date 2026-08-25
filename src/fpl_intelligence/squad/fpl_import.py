"""Phase 18.0 — One-click FPL squad importer.

All FPL API traffic is routed through :class:`FplEgressChain`, which tries a
direct fetch followed by a sequence of CORS-mask fallbacks (allorigins,
corsproxy.io, then the user's ``$FPL_PROXY_URL`` Apps-Script proxy). Each
strategy has a short timeout and validates the JSON shape before accepting it,
so a blocked mask fails fast and falls through to the next one. The winning
strategy is logged and surfaced in the ``sync_status`` line the dashboard
renders, so the user always knows which path reached FPL.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.config import get_settings
from fpl_intelligence.data_providers.fpl_egress import (
    FplEgressChain,
    FplEgressError,
    FplEgressExhaustedError,
    validate_bootstrap_payload,
    validate_entry_payload,
    validate_picks_payload,
)
from fpl_intelligence.db.models import Player, PlayerExternalId
from fpl_intelligence.squad.models import SquadStateCreate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# v2.5.6 — bootstrap (10m) + picks (60s) in-process caches for warm retry
# ---------------------------------------------------------------------------
_BOOTSTRAP_TTL = 600.0
_PICKS_TTL = 60.0

_BOOTSTRAP_CACHE: tuple[float, dict[str, Any]] | None = None
_BOOTSTRAP_LOCK = threading.Lock()

_PICKS_CACHE: dict[tuple[int, int], tuple[float, dict[str, Any]]] = {}
_PICKS_LOCK = threading.Lock()


def _get_cached_bootstrap() -> dict[str, Any] | None:
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAP_CACHE is None:
            return None
        at, payload = _BOOTSTRAP_CACHE
        if time.monotonic() - at < _BOOTSTRAP_TTL:
            return dict(payload) if isinstance(payload, dict) else payload
        return None


def _set_cached_bootstrap(payload: dict[str, Any]) -> None:
    with _BOOTSTRAP_LOCK:
        globals()["_BOOTSTRAP_CACHE"] = (time.monotonic(), dict(payload) if isinstance(payload, dict) else payload)


def _get_cached_picks(entry_id: int, gw: int) -> dict[str, Any] | None:
    key = (int(entry_id), int(gw))
    with _PICKS_LOCK:
        hit = _PICKS_CACHE.get(key)
        if hit:
            at, payload = hit
            if time.monotonic() - at < _PICKS_TTL:
                return dict(payload) if isinstance(payload, dict) else payload
            _PICKS_CACHE.pop(key, None)
    return None


def _set_cached_picks(entry_id: int, gw: int, payload: dict[str, Any]) -> None:
    key = (int(entry_id), int(gw))
    with _PICKS_LOCK:
        _PICKS_CACHE[key] = (time.monotonic(), dict(payload) if isinstance(payload, dict) else payload)


def clear_fpl_import_caches() -> None:
    """Clear bootstrap + picks caches (tests only)."""
    with _BOOTSTRAP_LOCK:
        globals()["_BOOTSTRAP_CACHE"] = None
    with _PICKS_LOCK:
        _PICKS_CACHE.clear()


async def fetch_official_history_for_gw(
    entry_id: int, gw: int, egress: FplEgressChain | None = None
) -> dict[str, Any] | None:
    """Fetch the official FPL history for a specific gameweek.

    Returns the history block for the given GW with event_transfers count
    and transfers array, or None if unavailable.
    """
    from fpl_intelligence.config import get_settings
    from fpl_intelligence.data_providers.fpl_egress import FplEgressChain

    if egress is None:
        settings = get_settings()
        egress = FplEgressChain(
            settings.fpl_base_url,
            timeout=settings.egress_strategy_timeout,
            cache_ttl=300.0,
        )

    def _validate(data: Any) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("history"), list):
            raise ValueError(
                f"history payload missing 'history' list (got {type(data).__name__})"
            )

    try:
        payload = await egress.fetch(
            f"/api/entry/{int(entry_id)}/history/", validator=_validate
        )
    except Exception as exc:
        logger.debug("fetch_official_history_for_gw failed for entry=%s: %s", entry_id, exc)
        return None

    for block in (payload or {}).get("history") or []:
        if not isinstance(block, dict):
            continue
        try:
            if int(block.get("event") or 0) == int(gw):
                return {
                    "event": block.get("event"),
                    "event_transfers": block.get("event_transfers"),
                    "event_transfers_cost": block.get("event_transfers_cost"),
                    "transfers": block.get("transfers") or [],
                }
        except (TypeError, ValueError):
            continue
    return None


_FPL_ELEMENT_TYPE_TO_POSITION = {1: 1, 2: 2, 3: 3, 4: 4}

_FPL_CHIP_TO_INTERNAL = {
    "wildcard": "wildcard",
    "freehit": "free_hit",
    "bboost": "bench_boost",
    "3xc": "triple_captain",
}

_DEFAULT_CHIPS = ["wildcard", "free_hit", "bench_boost", "triple_captain"]

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class FplImportError(Exception):
    """Base class for recoverable FPL import failures."""


class FplEntryNotFound(FplImportError):
    """The entry ID does not exist on FPL (HTTP 404 from the entry endpoint)."""


class FplApiUnavailable(FplImportError):
    """The FPL API is unreachable / returning server errors."""


class FplRateLimitBlocked(FplImportError):
    """FPL API blocked by rate limit (HTTP 403)."""


class FplPicksNotSaved(FplImportError):
    """Picks not saved yet (HTTP 404)."""


class FplPicksUnavailable(FplImportError):
    """The entry exists but its picks are unavailable (pre-season).

    The entry lookup succeeded, so we know the manager's team name, but
    ``/event/<gw>/picks/`` returns 404 because the new season hasn't opened.
    The UI turns this into a friendly "pre-season" message instead of a scary
    error.
    """

    def __init__(self, entry_name: str | None, gameweek: int) -> None:
        self.entry_name = entry_name
        self.gameweek = gameweek
        name = entry_name or "your team"
        message = (
            f"We found your team ({name})! The new FPL season hasn't opened yet, "
            f"so your players aren't visible. Try the Demo Team below, or check "
            f"back after the first deadline."
        )
        super().__init__(message)


class FplImportResult:
    """Holds everything the route needs to persist and render a squad."""

    def __init__(
        self,
        *,
        squad: SquadStateCreate,
        player_names: dict[int, str],
        entry_name: str | None,
        gameweek: int,
        winning_strategy: str | None = None,
        egress_attempts: list[tuple[str, str]] | None = None,
        rebuilt_from_history: bool = False,
        pending_transfer_gw: int | None = None,
        no_pending_transfer: bool = False,
    ) -> None:
        self.squad = squad
        self.player_names = player_names
        self.entry_name = entry_name
        self.gameweek = gameweek
        self.winning_strategy = winning_strategy
        self.egress_attempts = egress_attempts or []
        #: v2.6.0 — True when the squad was synthesised from official history
        #: element_in/out applied to the saved snapshot ("rebuilt" quirk path).
        self.rebuilt_from_history = rebuilt_from_history
        #: Target GW the transfer saga is about (= resolved next unplayed GW).
        self.pending_transfer_gw = pending_transfer_gw
        #: v2.6.0 — True when picks_next was 404 AND official history shows
        #: zero confirmed transfers for the target GW (honest banner case).
        self.no_pending_transfer = no_pending_transfer


class FplSquadImporter:
    """Fetch a manager's squad from the official FPL API through the egress chain.

    All three FPL endpoints (entry, picks, bootstrap) are fetched through a
    :class:`FplEgressChain` when one is supplied, so a blocked direct path
    automatically falls through to CORS masks and the user's Apps-Script proxy.
    The winning strategy is exposed via :attr:`last_winning_strategy` so the
    dashboard's sync-status line can tell the user exactly which path worked.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
        egress: FplEgressChain | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.fpl_base_url).rstrip("/")
        self._timeout = timeout if timeout is not None else settings.request_timeout_seconds
        self._client = client
        self._egress = egress
        self._last_winning_strategy: str | None = None
        self._last_egress_attempts: list[tuple[str, str]] = []

    @property
    def last_winning_strategy(self) -> str | None:
        return self._last_winning_strategy

    @property
    def last_egress_attempts(self) -> list[tuple[str, str]]:
        return list(self._last_egress_attempts)

    async def build_squad_from_entry(
        self, entry_id: int, db: Session | None = None, *, force_next_gw: bool = False
    ) -> FplImportResult:
        """Resolve an FPL entry ID into a :class:`FplImportResult`.

        v2.5.3 truth: fetch picks for *both* current_event AND the next
        unplayed GW (gameweek_clock). Store whichever differs from the saved
        snapshot (prefer next when both differ) so a pre-deadline transfer is
        never lost. The chosen GW is recorded as ``picks_gw`` / ``gameweek``.

        v2.5.4: when ``force_next_gw`` is True (dashboard toggle), the next-GW
        picks are returned even when they match the current GW — the user
        explicitly wants the pre-deadline transfer window.

        v2.5.6: bootstrap cached 10 min; entry + picks_* fetched in parallel
        via asyncio.gather; raw picks cached 60s for warm retry.
        """
        logger.info("build_squad_from_entry: START entry_id=%s db=%s", entry_id, db is not None)
        try:
            # --- bootstrap cache (10m) + parallel entry/bootstrap when cold ----
            bootstrap = _get_cached_bootstrap()
            if bootstrap is None:
                # Cold: fetch entry + bootstrap in parallel
                entry, bootstrap = await asyncio.gather(
                    self._fetch_json(
                        f"/api/entry/{entry_id}/",
                        validator=validate_entry_payload,
                    ),
                    self._fetch_json(
                        "/api/bootstrap-static/",
                        validator=validate_bootstrap_payload,
                    ),
                )
                _set_cached_bootstrap(bootstrap)
                logger.info(
                    "build_squad_from_entry: bootstrap fetched elements=%s",
                    len(bootstrap.get("elements", [])),
                )
            else:
                entry = await self._fetch_json(
                    f"/api/entry/{entry_id}/",
                    validator=validate_entry_payload,
                )
                logger.info(
                    "build_squad_from_entry: bootstrap cache hit elements=%s",
                    len(bootstrap.get("elements", [])),
                )
            current_gw = int(entry.get("current_event") or 1)
            entry_name = entry.get("name")
            logger.info(
                "build_squad_from_entry: entry fetched current_gw=%s name=%s",
                current_gw,
                entry_name,
            )
            # v2.5.3: resolve next unplayed GW via gameweek_clock (bootstrap
            # next-deadline), fallback to current+1 when unavailable.
            next_gw: int | None = None
            try:
                from fpl_intelligence.sync.gameweek_clock import pick_target_event

                events = bootstrap.get("events") if isinstance(bootstrap, dict) else None
                if isinstance(events, list) and events:
                    next_gw = pick_target_event(events)
                    if next_gw is not None and int(next_gw) == int(current_gw):
                        pass
                    if next_gw is None:
                        next_gw = int(current_gw) + 1
                else:
                    next_gw = int(current_gw) + 1
            except Exception as exc:  # noqa: BLE001 — never fail import on clock
                logger.debug("next_gw resolve failed: %s", exc)
                next_gw = int(current_gw) + 1

            # --- picks: warm cache 60s + parallel gather -----------------------
            picks_current: dict[str, Any] | None = None
            picks_next: dict[str, Any] | None = None

            cached_current = _get_cached_picks(int(entry_id), int(current_gw))
            cached_next = None
            if next_gw is not None and int(next_gw) != int(current_gw):
                cached_next = _get_cached_picks(int(entry_id), int(next_gw))

            # Helper: safe fetch that returns payload or exception instance
            async def _safe_fetch_picks(gw: int) -> Any:
                try:
                    payload = await self._fetch_json(
                        f"/api/entry/{entry_id}/event/{gw}/picks/",
                        validator=validate_picks_payload,
                    )
                    _set_cached_picks(int(entry_id), int(gw), payload)
                    logger.info(
                        "build_squad_from_entry: picks gw=%s count=%s",
                        gw,
                        len(payload.get("picks", [])),
                    )
                    return payload
                except Exception as exc:  # noqa: BLE001 — caller classifies
                    return exc

            # Determine which GWs need network
            need_current = cached_current is None
            need_next = cached_next is None and next_gw is not None and int(next_gw) != int(current_gw)

            if need_current and need_next:
                results: list[Any] = await asyncio.gather(
                    _safe_fetch_picks(int(current_gw)),
                    _safe_fetch_picks(int(next_gw)),  # type: ignore[arg-type]
                )
                r_cur, r_next = results
                if isinstance(r_cur, Exception):
                    if isinstance(r_cur, FplPicksNotSaved):
                        if next_gw is None or int(next_gw) == int(current_gw):
                            raise r_cur
                        logger.info("picks for current_gw=%s not saved, will try next_gw=%s", current_gw, next_gw)
                        picks_current = None
                    else:
                        # For non-404 errors on current, treat as unavailable but allow next to win
                        logger.info("picks current_gw fetch error: %s", r_cur)
                        picks_current = None
                else:
                    picks_current = r_cur
                if isinstance(r_next, Exception):
                    if isinstance(r_next, (FplPicksNotSaved, FplApiUnavailable, FplEgressError)):
                        logger.info("picks for next_gw=%s unavailable (%s)", next_gw, type(r_next).__name__)
                    else:
                        logger.info("picks next_gw fetch error: %s", r_next)
                    picks_next = None
                else:
                    picks_next = r_next
                if cached_current is not None and picks_current is None and not isinstance(r_cur, Exception):
                    picks_current = cached_current
                if cached_next is not None and picks_next is None and not isinstance(r_next, Exception):
                    picks_next = cached_next
            elif need_current:
                r = await _safe_fetch_picks(int(current_gw))
                if isinstance(r, Exception):
                    if isinstance(r, FplPicksNotSaved):
                        if next_gw is None or int(next_gw) == int(current_gw):
                            raise r
                        logger.info("picks for current_gw=%s not saved, will try next_gw=%s", current_gw, next_gw)
                        picks_current = None
                    else:
                        logger.info("picks current_gw fetch error: %s", r)
                        picks_current = None
                else:
                    picks_current = r
                picks_next = cached_next
            elif need_next:
                picks_current = cached_current
                r2 = await _safe_fetch_picks(int(next_gw))  # type: ignore[arg-type]
                if isinstance(r2, Exception):
                    if isinstance(r2, (FplPicksNotSaved, FplApiUnavailable, FplEgressError)):
                        logger.info("picks for next_gw=%s unavailable (%s)", next_gw, type(r2).__name__)
                    else:
                        logger.info("picks next_gw fetch error: %s", r2)
                    picks_next = None
                else:
                    picks_next = r2
            else:
                picks_current = cached_current
                picks_next = cached_next

            # Choose which payload to keep (v2.5.4 force_next_gw toggle).
            picks_payload, gameweek = self._choose_picks_payload(
                current_gw=int(current_gw),
                next_gw=int(next_gw) if next_gw is not None else None,
                picks_current=picks_current,
                picks_next=picks_next,
                db=db,
                entry_id=int(entry_id),
                force_next_gw=bool(force_next_gw),
            )

            # --- v2.6.0 truth branches --------------------------------------
            # Branch A (picks_next 200 + differs) is handled by
            # _choose_picks_payload: the differing next-GW payload is returned
            # and saved below. Branches B/C cover the FPL mid-window quirk:
            # picks_next 404 while official history/transfers already record
            # element_in/out for the target GW.
            rebuilt_from_history = False
            no_pending_transfer = False

            next_unavailable = picks_next is None and (
                next_gw is not None and int(next_gw) != int(current_gw)
            )
            chose_current = picks_payload is picks_current or picks_payload is cached_current

            if picks_payload is None and db is not None:
                # Both picks payloads missing — try rebuilding from official
                # history before giving up.
                rebuilt = await self._try_rebuild_from_history(
                    db=db, entry_id=int(entry_id), bootstrap=bootstrap, target_gw=next_gw
                )
                if rebuilt is not None:
                    picks_payload, gameweek = rebuilt
                    rebuilt_from_history = True
            elif next_unavailable and chose_current and not bool(force_next_gw):
                saved_row_ids = self._load_saved_ids(db, int(entry_id))
                if saved_row_ids:
                    rebuilt = await self._try_rebuild_from_history(
                        db=db,
                        entry_id=int(entry_id),
                        bootstrap=bootstrap,
                        target_gw=next_gw,
                        saved_ids=saved_row_ids,
                        base_picks=picks_payload,
                    )
                    if rebuilt is not None:
                        picks_payload, gameweek = rebuilt
                        rebuilt_from_history = True
                    else:
                        # Branch C: nothing confirmed on FPL for the target GW.
                        no_pending_transfer = True

            if picks_payload is None:
                # Both missing — surface the original error.
                raise FplPicksNotSaved(f"No picks available for entry {entry_id}")
            logger.info("build_squad_from_entry: chosen picks_gw=%s", gameweek)
        except (FplImportError, FplEgressError):
            raise
        except (httpx.HTTPStatusError, httpx.HTTPError, ValueError) as exc:
            raise FplApiUnavailable(str(exc)) from exc

        logger.info("build_squad_from_entry: calling _build_result")
        try:
            result = self._build_result(
                entry=entry,
                picks_payload=picks_payload,
                bootstrap=bootstrap,
                gameweek=gameweek,
                entry_name=entry_name,
                db=db,
            )
            result.winning_strategy = self._last_winning_strategy
            result.egress_attempts = list(self._last_egress_attempts)
            result.rebuilt_from_history = rebuilt_from_history
            result.pending_transfer_gw = int(next_gw) if next_gw is not None else None
            result.no_pending_transfer = no_pending_transfer
            logger.info(
                "build_squad_from_entry: _build_result OK player_names=%s",
                len(result.player_names),
            )
            return result
        except FplImportError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert unexpected errors to FplApiUnavailable
            logger.exception("build_squad_from_entry: _build_result FAILED: %s", exc)
            raise FplApiUnavailable(
                f"FPL response parse failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _choose_picks_payload(
        self,
        *,
        current_gw: int,
        next_gw: int | None,
        picks_current: dict[str, Any] | None,
        picks_next: dict[str, Any] | None,
        db: Session | None,
        entry_id: int,
        force_next_gw: bool = False,
    ) -> tuple[dict[str, Any] | None, int]:
        """Pick the truth payload between current and next GW picks.

        Rules (v2.5.3):
        * If only one payload exists → use it.
        * If both exist and a saved snapshot exists → prefer whichever
          *differs* from the saved snapshot (if only one differs, that one;
          if both differ, prefer the *next* GW — latest transfer).
        * If no saved snapshot → prefer next GW when it differs from current
          (captures a pre-deadline transfer), otherwise current.
        v2.5.4: when ``force_next_gw`` is True, return the next-GW payload
        unconditionally (if it exists) — the dashboard toggle forces picks_gw
        to next_gw regardless of current_event.
        Returns (payload, picks_gw).
        """
        # Helper to extract element ids set.
        def _ids(payload: dict[str, Any] | None) -> set[int] | None:
            if not payload or not isinstance(payload.get("picks"), list):
                return None
            try:
                return {int(p["element"]) for p in payload["picks"] if isinstance(p, dict) and "element" in p}
            except Exception:
                return None

        ids_current = _ids(picks_current)
        ids_next = _ids(picks_next)

        # v2.5.4: forced next-GW toggle — honour the checkbox from the dashboard.
        if force_next_gw and picks_next is not None:
            return picks_next, int(next_gw or current_gw)

        # Single payload cases
        if picks_current is None and picks_next is None:
            return None, current_gw
        if picks_current is None:
            return picks_next, int(next_gw or current_gw)
        if picks_next is None:
            return picks_current, int(current_gw)

        # Both payloads present
        # Try to load saved snapshot ids.
        saved_ids: set[int] | None = None
        if db is not None:
            try:
                from fpl_intelligence.squad.models_db import SquadStateDB

                row = db.scalar(
                    select(SquadStateDB).where(SquadStateDB.session_id == str(entry_id))
                )
                if row is not None and isinstance(row.squad_json, dict):
                    pids = row.squad_json.get("player_ids") or []
                    saved_ids = {int(pid) for pid in pids if isinstance(pid, (int, str)) and str(pid).isdigit()}
                    # Also handle ints directly
                    if not saved_ids and pids:
                        try:
                            saved_ids = {int(x) for x in pids}
                        except Exception:
                            saved_ids = None
            except Exception as exc:  # noqa: BLE001 — never fail choice on DB read
                logger.debug("saved snapshot read failed for choice: %s", exc)

        if saved_ids is not None and ids_current is not None and ids_next is not None:
            diff_current = ids_current != saved_ids
            diff_next = ids_next != saved_ids
            if diff_next and not diff_current:
                return picks_next, int(next_gw or current_gw)
            if diff_current and not diff_next:
                return picks_current, int(current_gw)
            if diff_next and diff_current:
                # Both differ — prefer next (latest transfer)
                return picks_next, int(next_gw or current_gw)
            # Neither differs — no transfer, keep current
            return picks_current, int(current_gw)

        # No saved snapshot — prefer next when it differs from current
        if ids_current is not None and ids_next is not None and ids_current != ids_next:
            return picks_next, int(next_gw or current_gw)
        # Default to current (or next if current missing)
        return picks_current, int(current_gw)

    @staticmethod
    def _load_saved_ids(db: Session | None, entry_id: int) -> list[int] | None:
        """Saved snapshot player ids for ``entry_id`` (None when absent)."""
        if db is None:
            return None
        try:
            from fpl_intelligence.squad.models_db import SquadStateDB

            row = db.scalar(select(SquadStateDB).where(SquadStateDB.session_id == str(entry_id)))
            if row is not None and isinstance(row.squad_json, dict):
                pids = row.squad_json.get("player_ids") or []
                try:
                    out = [int(x) for x in pids]
                    return out or None
                except (TypeError, ValueError):
                    return None
        except Exception as exc:  # noqa: BLE001 — never fail choice on DB read
            logger.debug("saved snapshot read failed: %s", exc)
        return None

    async def _try_rebuild_from_history(
        self,
        *,
        db: Session | None,
        entry_id: int,
        bootstrap: dict[str, Any],
        target_gw: int | None,
        saved_ids: list[int] | None = None,
        base_picks: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int] | None:
        """Branch B — rebuild the next-GW squad from official transfer history.

        When ``/event/{gw}/picks/`` 404s but official history already records
        element_in/out for that GW, apply those swaps to the saved squad and
        return a synthesised picks payload. Returns ``None`` when history
        shows zero confirmed transfers (branch C) or is unreachable.
        """
        from fpl_intelligence.squad.fpl_truth import (
            _fetch_confirmed_transfers,
            _fetch_history,
            rebuild_squad_ids_from_swaps,
        )

        rows = await asyncio.gather(
            _fetch_history(self, entry_id),
            _fetch_confirmed_transfers(self, entry_id),
        )
        history_rows, live_transfers = rows

        swaps: list[dict[str, Any]] = []
        seen_pairs: set[tuple[int, int]] = set()
        # Live endpoint rows carry their own ``event``; history-block rows
        # inherit the block's event.
        candidates: list[tuple[int, dict[str, Any]]] = [
            (int(tr.get("event") or 0), tr) for tr in live_transfers
        ]
        for block in history_rows:
            try:
                block_gw = int(block.get("event"))
            except (TypeError, ValueError):
                continue
            for tr in block.get("transfers") or []:
                if isinstance(tr, dict):
                    candidates.append((block_gw, tr))
        for ev, tr in candidates:
            if target_gw is not None and ev != int(target_gw):
                continue
            try:
                pair = (int(tr.get("element_in")), int(tr.get("element_out")))
            except (TypeError, ValueError):
                continue
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            swaps.append({"element_in": pair[0], "element_out": pair[1]})
        # History event row without element arrays still proves a transfer was
        # made only via its transfers array; an empty array means nothing.
        if not swaps:
            logger.info(
                "rebuild-from-history: no confirmed swaps for gw=%s entry=%s",
                target_gw,
                entry_id,
            )
            return None

        base_ids: list[int]
        captain_id: int | None = None
        vice_captain_id: int | None = None
        if base_picks and isinstance(base_picks.get("picks"), list):
            base_ids = [int(p["element"]) for p in base_picks["picks"] if isinstance(p, dict) and "element" in p]
            captain_id = next(
                (int(p["element"]) for p in base_picks["picks"] if isinstance(p, dict) and p.get("is_captain")),
                None,
            )
            vice_captain_id = next(
                (int(p["element"]) for p in base_picks["picks"] if isinstance(p, dict) and p.get("is_vice_captain")),
                None,
            )
        else:
            loaded = saved_ids if saved_ids is not None else self._load_saved_ids(db, entry_id)
            if not loaded:
                return None
            base_ids = list(loaded)
            if db is not None:
                try:
                    from fpl_intelligence.squad.models_db import SquadStateDB

                    row = db.scalar(
                        select(SquadStateDB).where(SquadStateDB.session_id == str(entry_id))
                    )
                    if row is not None and isinstance(row.squad_json, dict):
                        captain_id = row.squad_json.get("captain_id")
                        vice_captain_id = row.squad_json.get("vice_captain_id")
                        captain_id = int(captain_id) if captain_id else None
                        vice_captain_id = int(vice_captain_id) if vice_captain_id else None
                except Exception as exc:  # noqa: BLE001 — display-only fallback
                    logger.debug("captain read failed during rebuild: %s", exc)

        rebuilt = rebuild_squad_ids_from_swaps(base_ids, swaps)
        if rebuilt is None:
            return None
        new_ids, ins_applied, outs_removed = rebuilt

        # Captain continuity: promote vice when the captain left, first XI
        # member when both left.
        elements = {int(e["id"]): e for e in bootstrap.get("elements") or []}
        if captain_id in outs_removed:
            captain_id = vice_captain_id if vice_captain_id in new_ids else next(iter(new_ids), None)
        elif captain_id not in new_ids:
            captain_id = next(iter(new_ids), None)
        if vice_captain_id not in new_ids:
            vice_captain_id = next((p for p in new_ids if p != captain_id), None)

        def pick_meta(el: int) -> dict[str, Any]:
            meta = {"element": el, "position": len(new_ids), "is_captain": el == captain_id, "is_vice_captain": el == vice_captain_id}
            meta["multiplier"] = 2 if el == captain_id else 1
            return meta

        payload: dict[str, Any] = {
            "picks": [pick_meta(el) for el in new_ids],
            "entry_history": {},
            "transfers": base_picks.get("transfers", {}) if isinstance(base_picks, dict) else {},
            "_rebuilt_from_history": True,
            "_swaps": [{"in": i, "out": o} for i, o in zip(ins_applied, outs_removed)],
        }
        _ = elements  # names resolve later through _build_result/bootstrap
        logger.info(
            "rebuild-from-history: applied %s swap(s) for gw=%s entry=%s ins=%s outs=%s",
            len(ins_applied),
            target_gw,
            entry_id,
            ins_applied,
            outs_removed,
        )
        return payload, int(target_gw or 0)

    async def _fetch_json(
        self,
        path: str,
        *,
        validator: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        """Fetch a path through the egress chain, or fall back to a direct client.

        The egress chain handles 403/429/500 from FPL by trying each mask. When
        every strategy fails we translate the exhaustion into the importer's
        typed error hierarchy so the route can return a truthful 503.
        """
        if self._egress is not None:
            try:
                data = await self._egress.fetch(path, validator=validator)
            except FplEgressExhaustedError as exc:
                self._last_egress_attempts = exc.attempts
                self._last_winning_strategy = None
                # When every mask was tried, check if the failure looks like a 404
                # for picks/entry paths — those should surface as typed errors so
                # the caller can try the next GW instead of failing the whole sync.
                attempts_text = " ".join(err for _, err in exc.attempts)
                if "/picks/" in path and ("404" in attempts_text or "Not found" in attempts_text):
                    raise FplPicksNotSaved(f"Picks not found for {path}: {exc}") from exc
                if path.startswith("/api/entry/") and path.endswith("/") and ("404" in attempts_text):
                    raise FplEntryNotFound(f"FPL entry not found: {path}") from exc
                # A 404 from *every* mask is genuinely a missing entry, not a
                # block — but masks rarely 404, so treat exhaustion as blocked.
                raise FplApiUnavailable(str(exc)) from exc
            self._last_winning_strategy = self._egress.winning_strategy
            self._last_egress_attempts = []
            return data

        # Direct path (no egress chain supplied): preserve legacy behaviour.
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True, headers=_BROWSER_HEADERS
        )
        try:
            response = await client.get(f"{self._base_url}{path}")
            if response.status_code == 403:
                raise FplRateLimitBlocked("FPL API blocked by rate limit")
            if response.status_code == 404:
                if path.startswith("/api/entry/") and path.endswith("/"):
                    raise FplEntryNotFound(f"FPL entry not found: {path}")
                if "/picks/" in path:
                    raise FplPicksNotSaved("Picks not saved yet")
                raise FplApiUnavailable(f"FPL resource not found: {path}")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise FplApiUnavailable(f"Unexpected FPL response for {path}")
            self._last_winning_strategy = "direct"
            return data
        finally:
            if own_client:
                await client.aclose()

    def _build_result(
        self,
        *,
        entry: dict[str, Any],
        picks_payload: dict[str, Any],
        bootstrap: dict[str, Any],
        gameweek: int,
        entry_name: str | None,
        db: Session | None,
    ) -> FplImportResult:
        picks: list[dict[str, Any]] = picks_payload.get("picks") or []
        transfers: dict[str, Any] = picks_payload.get("transfers") or {}

        if len(picks) != 15:
            raise FplApiUnavailable(f"Expected 15 picks from FPL, got {len(picks)}")

        element_ids = [int(p["element"]) for p in picks]
        captain_id = next((int(p["element"]) for p in picks if p.get("is_captain")), element_ids[0])
        vice_captain_id = next(
            (int(p["element"]) for p in picks if p.get("is_vice_captain")), element_ids[1]
        )

        elements = {int(e["id"]): e for e in (bootstrap.get("elements") or [])}
        player_positions: dict[int, int] = {}
        player_prices: dict[int, float] = {}
        player_teams: dict[int, int] = {}
        bootstrap_names: dict[int, str] = {}
        for el in element_ids:
            meta = elements.get(el, {})
            player_positions[el] = _FPL_ELEMENT_TYPE_TO_POSITION.get(
                int(meta.get("element_type", 0)) or 1, 1
            )
            player_prices[el] = float(meta.get("now_cost", 0)) / 10.0
            player_teams[el] = int(meta.get("team", 0)) or 0
            bootstrap_names[el] = meta.get("web_name") or f"Player {el}"

        bank_tenths = transfers.get("bank")
        if bank_tenths is None:
            bank_tenths = entry.get("last_deadline_bank", 0)
        bank = float(bank_tenths) / 10.0
        free_transfers = int(
            transfers.get("limit", entry.get("last_deadline_total_transfers", 1) or 1)
        )

        chips_available = self._resolve_available_chips(entry)

        player_names = self._resolve_player_names(element_ids, bootstrap_names, db)

        squad = SquadStateCreate(
            player_ids=element_ids,
            captain_id=captain_id,
            vice_captain_id=vice_captain_id,
            bank=bank,
            free_transfers=free_transfers,
            chips_available=chips_available,
            gameweek=gameweek,
            picks_gw=gameweek,
            player_positions=player_positions,
            player_prices=player_prices,
            player_teams=player_teams,
        )
        return FplImportResult(
            squad=squad,
            player_names=player_names,
            entry_name=entry_name,
            gameweek=gameweek,
        )

    @staticmethod
    def _resolve_available_chips(entry: dict[str, Any]) -> list[str]:
        chips = entry.get("chips")
        if not isinstance(chips, list):
            return list(_DEFAULT_CHIPS)
        available: list[str] = []
        for chip in chips:
            code = chip.get("chip")
            if code is None:
                continue
            if chip.get("event") is not None:
                continue
            internal = _FPL_CHIP_TO_INTERNAL.get(code)
            if internal is not None and internal not in available:
                available.append(internal)
        return available or list(_DEFAULT_CHIPS)

    @staticmethod
    def _resolve_player_names(
        element_ids: list[int],
        bootstrap_names: dict[int, str],
        db: Session | None,
    ) -> dict[int, str]:
        names: dict[int, str] = {}
        logger.info(
            "_resolve_player_names: START element_ids=%s db=%s",
            element_ids,
            db is not None,
        )
        for el in element_ids:
            name = bootstrap_names.get(el) or f"Player {el}"
            if db is not None:
                # Primary: direct join on the official FPL element id column
                # (never our internal auto-increment id). Fallback: the legacy
                # external-id mapping table.
                logger.info("_resolve_player_names: querying fpl_element_id=%s", el)
                try:
                    player = db.scalar(select(Player).where(Player.fpl_element_id == el))
                    logger.info(
                        "_resolve_player_names: fpl_element_id=%s match=%s",
                        el,
                        player is not None,
                    )
                except Exception as exc:
                    logger.exception("_resolve_player_names: query FAILED for el=%s: %s", el, exc)
                    raise
                if player is None:
                    for ext_provider in ("official_fpl", "fpl"):
                        logger.info(
                            "_resolve_player_names: trying provider=%s el=%s",
                            ext_provider,
                            el,
                        )
                        ext = db.execute(
                            select(PlayerExternalId).where(
                                PlayerExternalId.provider == ext_provider,
                                PlayerExternalId.provider_player_id == str(el),
                            )
                        ).scalar_one_or_none()
                        if ext is not None:
                            player = db.get(Player, ext.player_id)
                            logger.info(
                                "_resolve_player_names: external resolved el=%s -> player_id=%s",
                                el,
                                ext.player_id,
                            )
                            break
                if player is not None:
                    name = player.web_name
            names[el] = name
        logger.info("_resolve_player_names: DONE names=%s", names)
        return names
