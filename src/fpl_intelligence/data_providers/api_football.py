"""Phase 11.1 — API-Football connector (requires API_FOOTBALL_KEY).

Supports:

* fixtures by date (``GET /fixtures?date=...``)
* lineups by fixture (``GET /fixtures/lineups?fixture=...``) — the confirmed
  starting XI and bench, the strongest possible source of ``start_probability``
  / ``expected_minutes`` facts.
* injuries (``GET /injuries?date=...`` or ``?team=&season=``).

The key is read **only** from the ``API_FOOTBALL_KEY`` environment variable (or
a constructor override). If the key is missing the connector disables itself
gracefully — :meth:`is_enabled` returns ``False`` and a clear warning is logged
once at construction. Every other layer treats a disabled connector as
"no facts", never as an error.

All network access is cache-first and testable via an injected
``httpx.MockTransport``; no live call is ever made inside ``pytest``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from fpl_intelligence.data_providers.base import (
    BaseDataConnector,
    DataConnectionError,
    DataConnectorError,
    DataParseError,
    DataProviderDisabledError,
)
from fpl_intelligence.data_providers.facts import FactSource, PlayerFact

logger = logging.getLogger(__name__)

#: API-Football v3 base URL.
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_KEY_ENV = "API_FOOTBALL_KEY"

#: API-Football injury ``type`` values that mean the player cannot start.
_INJURY_TYPES = {"injured", "missing", "suspended", "questionable", "doubtful"}


def _require_key(api_key: str | None) -> str:
    if not api_key:
        raise DataProviderDisabledError(
            "ApiFootballConnector disabled: set API_FOOTBALL_KEY to enable."
        )
    return api_key


def _response_list(payload: Any, *, what: str) -> list[dict[str, Any]]:
    """Extract the ``response`` array from an API-Football envelope."""
    if not isinstance(payload, dict):
        raise DataParseError(f"API-Football {what} payload must be a JSON object")
    if payload.get("errors"):
        errors = payload["errors"]
        raise DataConnectionError(f"API-Football {what} error: {errors}")
    response = payload.get("response")
    if not isinstance(response, list):
        raise DataParseError(f"API-Football {what} payload missing 'response' list")
    return response


def parse_lineups(
    payload: dict[str, Any],
    *,
    fpl_id_map: Mapping[int, int] | None = None,
) -> list[PlayerFact]:
    """Convert an API-Football lineups payload into starting/bench facts.

    Each team's ``startXI`` yields a confirmed starter (``is_starting``,
    ``expected_minutes=90``); each ``substitutes`` yields a confirmed bench
    player (``is_bench``, ``expected_minutes=0``). An optional ``fpl_id_map``
    maps API-Football player ids to FPL player ids so the fact can be keyed on
    the FPL id (entity resolution is otherwise owned by Phase 9.2.1).
    """
    fpl_map = fpl_id_map or {}
    now = datetime.utcnow()
    facts: list[PlayerFact] = []
    for team in _response_list(payload, what="lineups"):
        if not isinstance(team, dict):
            continue
        team_id = team.get("team", {}).get("id") if isinstance(team.get("team"), dict) else None
        team_name = team.get("team", {}).get("name") if isinstance(team.get("team"), dict) else None
        for slot in team.get("startXI", []) or []:
            player = (slot or {}).get("player") or {}
            af_id = player.get("id")
            facts.append(
                PlayerFact(
                    source=FactSource.API_FOOTBALL,
                    name=str(player.get("name") or "unknown"),
                    fpl_player_id=fpl_map.get(af_id) if af_id is not None else None,
                    api_football_player_id=af_id,
                    team_id=team_id,
                    team_name=team_name,
                    status="start",
                    is_starting=True,
                    expected_minutes=90,
                    raw=slot or {},
                    fetched_at=now,
                )
            )
        for slot in team.get("substitutes", []) or []:
            player = (slot or {}).get("player") or {}
            af_id = player.get("id")
            facts.append(
                PlayerFact(
                    source=FactSource.API_FOOTBALL,
                    name=str(player.get("name") or "unknown"),
                    fpl_player_id=fpl_map.get(af_id) if af_id is not None else None,
                    api_football_player_id=af_id,
                    team_id=team_id,
                    team_name=team_name,
                    status="bench",
                    is_bench=True,
                    expected_minutes=0,
                    raw=slot or {},
                    fetched_at=now,
                )
            )
    return facts


def parse_injuries(
    payload: dict[str, Any],
    *,
    fpl_id_map: Mapping[int, int] | None = None,
) -> list[PlayerFact]:
    """Convert an API-Football injuries payload into injured/out facts."""
    fpl_map = fpl_id_map or {}
    now = datetime.utcnow()
    facts: list[PlayerFact] = []
    for item in _response_list(payload, what="injuries"):
        if not isinstance(item, dict):
            continue
        player = item.get("player") or {}
        af_id = player.get("id")
        injury_type = str(item.get("type") or "").strip().lower()
        if injury_type and injury_type not in _INJURY_TYPES:
            # e.g. "Not injured" — not a fact we override on.
            continue
        team = item.get("team") or {}
        facts.append(
            PlayerFact(
                source=FactSource.API_FOOTBALL,
                name=str(player.get("name") or "unknown"),
                fpl_player_id=fpl_map.get(af_id) if af_id is not None else None,
                api_football_player_id=af_id,
                team_id=team.get("id"),
                team_name=team.get("name"),
                status="out" if injury_type in ("injured", "missing") else injury_type or "out",
                is_injured=True,
                raw=item,
                fetched_at=now,
            )
        )
    return facts


class ApiFootballConnector(BaseDataConnector):
    """Fetch fixtures, lineups and injuries from API-Football (keyed)."""

    name = "api_football"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = API_FOOTBALL_BASE,
        cache: Any = None,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
        min_interval_seconds: float = 1.0,
        clock: Any = None,
        monotonic_clock: Any = None,
        sleep: Any = None,
    ) -> None:
        resolved_key = api_key if api_key is not None else os.getenv(API_FOOTBALL_KEY_ENV)
        self._api_key = resolved_key
        self._disabled = not bool(resolved_key)
        self._base_url = base_url.rstrip("/")
        if self._disabled:
            logger.warning(
                "ApiFootballConnector disabled: %s is not set. Lineup/injury "
                "facts will be unavailable; the engine falls back to baseline "
                "predictions. Set %s to enable (free tier available).",
                API_FOOTBALL_KEY_ENV,
                API_FOOTBALL_KEY_ENV,
            )
        merged_headers = dict(headers or {})
        if resolved_key:
            merged_headers["x-apisports-key"] = resolved_key
        super().__init__(
            cache=cache,
            http_client=http_client,
            timeout=timeout,
            headers=merged_headers,
            min_interval_seconds=min_interval_seconds,
            clock=clock or __import__("time").monotonic,
            monotonic_clock=monotonic_clock or __import__("time").monotonic,
            sleep=sleep or __import__("time").sleep,
        )

    def is_enabled(self) -> bool:
        return not self._disabled

    def _require_enabled(self) -> None:
        if self._disabled:
            raise DataProviderDisabledError(
                "ApiFootballConnector is disabled (API_FOOTBALL_KEY not set)."
            )

    # -- network methods -----------------------------------------------------

    def fetch_fixtures_by_date(self, date: str) -> list[dict[str, Any]]:
        self._require_enabled()
        payload = self._get_json(f"{self._base_url}/fixtures", params={"date": date})
        return _response_list(payload, what="fixtures")

    def fetch_lineups(self, fixture_id: int) -> list[PlayerFact]:
        self._require_enabled()
        payload = self._get_json(
            f"{self._base_url}/fixtures/lineups",
            params={"fixture": fixture_id},
            sensitive=True,
        )
        return parse_lineups(payload)

    def fetch_injuries(
        self,
        *,
        date: str | None = None,
        team_id: int | None = None,
        season: int | None = None,
        fpl_id_map: Mapping[int, int] | None = None,
    ) -> list[PlayerFact]:
        self._require_enabled()
        params: dict[str, Any] = {}
        if date is not None:
            params["date"] = date
        if team_id is not None:
            params["team"] = team_id
        if season is not None:
            params["season"] = season
        if not params:
            raise DataProviderDisabledError(
                "fetch_injuries requires at least one of date/team_id/season."
            )
        payload = self._get_json(f"{self._base_url}/injuries", params=params, sensitive=True)
        return parse_injuries(payload, fpl_id_map=fpl_id_map)

    def fetch_confirmed_lineups(
        self, date: str, *, fpl_id_map: Mapping[int, int] | None = None
    ) -> list[PlayerFact]:
        """Fetch fixtures for ``date`` then the confirmed lineups for each.

        Returns the flattened list of starting/bench facts across all fixtures.
        """
        self._require_enabled()
        facts: list[PlayerFact] = []
        for fixture in self.fetch_fixtures_by_date(date):
            fid = (
                fixture.get("fixture", {}).get("id")
                if isinstance(fixture.get("fixture"), dict)
                else fixture.get("id")
            )
            if fid is None:
                continue
            facts.extend(self.fetch_lineups(fid))
        if fpl_id_map:
            for fact in facts:
                apid = fact.api_football_player_id
                if apid is not None and apid in fpl_id_map:
                    fact.fpl_player_id = fpl_id_map[apid]
        return facts

    def collect_player_facts(
        self,
        *,
        date: str | None = None,
        season: int | None = None,
        fpl_id_map: Mapping[int, int] | None = None,
    ) -> list[PlayerFact]:
        """Best-effort gathering of lineup + injury facts for the orchestrator.

        Returns ``[]`` when disabled; raises :class:`DataConnectorError` only on
        a genuine network/parse failure (the injector catches and degrades).
        """
        if self._disabled:
            return []
        facts: list[PlayerFact] = []
        if date is not None:
            try:
                facts.extend(self.fetch_confirmed_lineups(date, fpl_id_map=fpl_id_map))
            except DataConnectorError:
                logger.warning("API-Football lineup fetch failed; skipping.", exc_info=True)
            try:
                facts.extend(self.fetch_injuries(date=date, fpl_id_map=fpl_id_map))
            except DataConnectorError:
                logger.warning("API-Football injury fetch failed; skipping.", exc_info=True)
        return facts
