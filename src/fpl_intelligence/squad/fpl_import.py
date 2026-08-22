"""Phase 13.0 - One-click FPL squad importer."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.config import get_settings
from fpl_intelligence.db.models import Player, PlayerExternalId
from fpl_intelligence.squad.models import SquadStateCreate

logger = logging.getLogger(__name__)

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
    ) -> None:
        self.squad = squad
        self.player_names = player_names
        self.entry_name = entry_name
        self.gameweek = gameweek


class FplSquadImporter:
    """Fetch a manager's squad from the official FPL API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.fpl_base_url).rstrip("/")
        self._timeout = timeout if timeout is not None else settings.request_timeout_seconds
        self._client = client

    async def build_squad_from_entry(
        self, entry_id: int, db: Session | None = None
    ) -> FplImportResult:
        """Resolve an FPL entry ID into a :class:`FplImportResult`."""
        logger.info("build_squad_from_entry: START entry_id=%s db=%s", entry_id, db is not None)
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True, headers=_BROWSER_HEADERS
        )
        try:
            logger.info("build_squad_from_entry: fetching entry %s", entry_id)
            entry = await self._get_json(client, f"/api/entry/{entry_id}/")
            gameweek = entry.get("current_event") or 1
            entry_name = entry.get("name")
            logger.info("build_squad_from_entry: entry fetched gameweek=%s name=%s", gameweek, entry_name)

            try:
                logger.info("build_squad_from_entry: fetching picks for gw %s", gameweek)
                picks_payload = await self._get_json(
                    client, f"/api/entry/{entry_id}/event/{gameweek}/picks/"
                )
                logger.info("build_squad_from_entry: picks fetched count=%s", len(picks_payload.get("picks", [])))
            except FplPicksNotSaved:
                raise
            logger.info("build_squad_from_entry: fetching bootstrap")
            bootstrap = await self._get_json(client, "/api/bootstrap-static/")
            logger.info("build_squad_from_entry: bootstrap fetched elements=%s", len(bootstrap.get("elements", [])))
        except FplEntryNotFound:
            raise
        except (httpx.HTTPStatusError, httpx.HTTPError, ValueError) as exc:
            raise FplApiUnavailable(str(exc)) from exc
        finally:
            if own_client:
                await client.aclose()

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
            logger.info("build_squad_from_entry: _build_result OK player_names=%s", len(result.player_names))
            return result
        except FplImportError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert unexpected errors to FplApiUnavailable
            logger.exception("build_squad_from_entry: _build_result FAILED: %s", exc)
            raise FplApiUnavailable(f"FPL response parse failed: {type(exc).__name__}: {exc}") from exc

    async def _get_json(self, client: httpx.AsyncClient, path: str) -> dict[str, Any]:
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
        return data

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
            raise FplApiUnavailable(
                f"Expected 15 picks from FPL, got {len(picks)}"
            )

        element_ids = [int(p["element"]) for p in picks]
        captain_id = next(
            (int(p["element"]) for p in picks if p.get("is_captain")), element_ids[0]
        )
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
        logger.info("_resolve_player_names: START element_ids=%s db=%s", element_ids, db is not None)
        for el in element_ids:
            name = bootstrap_names.get(el) or f"Player {el}"
            if db is not None:
                # Primary: direct join on the official FPL element id column
                # (never our internal auto-increment id). Fallback: the legacy
                # external-id mapping table.
                logger.info("_resolve_player_names: querying fpl_element_id=%s", el)
                try:
                    player = db.scalar(select(Player).where(Player.fpl_element_id == el))
                    logger.info("_resolve_player_names: fpl_element_id=%s match=%s", el, player is not None)
                except Exception as exc:
                    logger.exception("_resolve_player_names: fpl_element_id query FAILED for el=%s: %s", el, exc)
                    raise
                if player is None:
                    for ext_provider in ("official_fpl", "fpl"):
                        logger.info("_resolve_player_names: trying external_id provider=%s el=%s", ext_provider, el)
                        ext = db.execute(
                            select(PlayerExternalId).where(
                                PlayerExternalId.provider == ext_provider,
                                PlayerExternalId.provider_player_id == str(el),
                            )
                        ).scalar_one_or_none()
                        if ext is not None:
                            player = db.get(Player, ext.player_id)
                            logger.info("_resolve_player_names: external_id resolved el=%s -> player_id=%s", el, ext.player_id)
                            break
                if player is not None:
                    name = player.web_name
            names[el] = name
        logger.info("_resolve_player_names: DONE names=%s", names)
        return names