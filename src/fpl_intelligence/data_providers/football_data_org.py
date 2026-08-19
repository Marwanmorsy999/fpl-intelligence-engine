"""Phase 11.1 — football-data.org connector (requires FOOTBALL_DATA_ORG_KEY).

Supports:

* competitions (``GET /v4/competitions``)
* competitions + matches (``GET /v4/competitions/{code}/matches``)
* standings (``GET /v4/competitions/{code}/standings``)

The token is read **only** from the ``FOOTBALL_DATA_ORG_KEY`` environment
variable (or a constructor override). If missing, the connector disables itself
gracefully — :meth:`is_enabled` returns ``False`` and a warning is logged. As
with API-Football, a disabled connector is treated as "no data", never as an
error.

football-data.org is primarily competition/match/standings data; it does not
expose per-player availability, so this connector contributes no
:class:`PlayerFact` rows (entity resolution to FPL players is out of scope and
owned by Phase 9.2.1). It exists so the engine can enrich match context and so
the API-first architecture is uniform across providers.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from fpl_intelligence.data_providers.base import (
    BaseDataConnector,
    DataParseError,
    DataProviderDisabledError,
)
from fpl_intelligence.data_providers.facts import PlayerFact

logger = logging.getLogger(__name__)

FOOTBALL_DATA_ORG_BASE = "https://api.football-data.org/v4"
FOOTBALL_DATA_ORG_KEY_ENV = "FOOTBALL_DATA_ORG_KEY"


@dataclass
class Competition:
    """A football-data.org competition (league/season)."""

    id: int
    name: str
    code: str | None
    area: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "area": self.area,
        }


@dataclass
class Match:
    """A single football-data.org match."""

    id: int
    utc_date: str | None
    status: str | None
    home_team: str | None
    away_team: str | None
    competition_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "utc_date": self.utc_date,
            "status": self.status,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "competition_code": self.competition_code,
        }


@dataclass
class StandingRow:
    """One row of a competition standings table."""

    position: int
    team: str | None
    points: int | None
    played: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "team": self.team,
            "points": self.points,
            "played": self.played,
        }


def parse_competitions(payload: dict[str, Any]) -> list[Competition]:
    if not isinstance(payload, dict):
        raise DataParseError("football-data competitions payload must be an object")
    competitions = payload.get("competitions")
    if not isinstance(competitions, list):
        raise DataParseError("football-data competitions payload missing list")
    out: list[Competition] = []
    for comp in competitions:
        if not isinstance(comp, dict):
            continue
        area = comp.get("area")
        out.append(
            Competition(
                id=int(comp["id"]),
                name=str(comp.get("name") or "unknown"),
                code=comp.get("code"),
                area=area.get("name") if isinstance(area, dict) else None,
            )
        )
    return out


def parse_matches(payload: dict[str, Any]) -> list[Match]:
    if not isinstance(payload, dict):
        raise DataParseError("football-data matches payload must be an object")
    matches = payload.get("matches")
    if not isinstance(matches, list):
        raise DataParseError("football-data matches payload missing list")
    out: list[Match] = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        comp = m.get("competition") or {}
        home = m.get("homeTeam") or {}
        away = m.get("awayTeam") or {}
        out.append(
            Match(
                id=int(m["id"]),
                utc_date=m.get("utcDate"),
                status=m.get("status"),
                home_team=home.get("name"),
                away_team=away.get("name"),
                competition_code=comp.get("code"),
            )
        )
    return out


def parse_standings(payload: dict[str, Any]) -> list[StandingRow]:
    if not isinstance(payload, dict):
        raise DataParseError("football-data standings payload must be an object")
    standings = payload.get("standings")
    if not isinstance(standings, list):
        raise DataParseError("football-data standings payload missing list")
    out: list[StandingRow] = []
    for table_group in standings:
        if not isinstance(table_group, dict):
            continue
        for row in table_group.get("table", []) or []:
            if not isinstance(row, dict):
                continue
            team = row.get("team") or {}
            out.append(
                StandingRow(
                    position=int(row.get("position", 0)),
                    team=team.get("name"),
                    points=row.get("points"),
                    played=row.get("playedGames"),
                )
            )
    return out


class FootballDataOrgConnector(BaseDataConnector):
    """Fetch competitions, matches and standings from football-data.org (token)."""

    name = "football_data_org"

    def __init__(
        self,
        *,
        api_token: str | None = None,
        base_url: str = FOOTBALL_DATA_ORG_BASE,
        cache: Any = None,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
        min_interval_seconds: float = 1.0,
        clock: Any = None,
        monotonic_clock: Any = None,
        sleep: Any = None,
    ) -> None:
        resolved = api_token if api_token is not None else os.getenv(FOOTBALL_DATA_ORG_KEY_ENV)
        self._token = resolved
        self._disabled = not bool(resolved)
        self._base_url = base_url.rstrip("/")
        if self._disabled:
            logger.warning(
                "FootballDataOrgConnector disabled: %s is not set. Competition/"
                "match/standings context will be unavailable; the engine falls "
                "back to baseline predictions. Set %s to enable (free tier "
                "available, 10 req/min).",
                FOOTBALL_DATA_ORG_KEY_ENV,
                FOOTBALL_DATA_ORG_KEY_ENV,
            )
        merged_headers = dict(headers or {})
        if resolved:
            merged_headers["X-Auth-Token"] = resolved
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
                "FootballDataOrgConnector is disabled (FOOTBALL_DATA_ORG_KEY not set)."
            )

    # -- network methods -----------------------------------------------------

    def fetch_competitions(self) -> list[Competition]:
        self._require_enabled()
        payload = self._get_json(f"{self._base_url}/competitions")
        return parse_competitions(payload)

    def fetch_matches(self, competition_code: str | None = None) -> list[Match]:
        self._require_enabled()
        if competition_code:
            url = f"{self._base_url}/competitions/{competition_code}/matches"
        else:
            url = f"{self._base_url}/matches"
        payload = self._get_json(url)
        return parse_matches(payload)

    def fetch_standings(self, competition_code: str) -> list[StandingRow]:
        self._require_enabled()
        url = f"{self._base_url}/competitions/{competition_code}/standings"
        payload = self._get_json(url)
        return parse_standings(payload)

    def collect_player_facts(self, **_: Any) -> list[PlayerFact]:
        """football-data.org has no per-player availability feed.

        Returns ``[]`` — the connector enriches match/competition context, not
        individual player availability. Always safe to call.
        """
        return []
