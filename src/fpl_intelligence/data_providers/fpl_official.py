"""Phase 11.1 — Official FPL API connector (no API key required).

Fetches the public Fantasy Premier League endpoints and normalises the core
facts the quantitative engine cares about:

* ``bootstrap-static`` — every player's ``news``, ``chance_of_playing_*``,
  ``status``, ``now_cost`` (price), ``team`` and any ``expected_minutes``.
* ``fixtures`` — upcoming gameweeks and fixture difficulty per team.
* ``element-summary/{player_id}`` — a single player's upcoming fixtures.

The connector produces :class:`~fpl_intelligence.data_providers.facts.PlayerFact`
objects (one per player) with a derived ``fixture_difficulty`` for the next
gameweek where derivable. It requires **no** API key and never hardcodes one.

All network access is routed through :meth:`BaseDataConnector._get_json`, which
is cache-first and testable via an injected ``httpx.MockTransport``. Per the
Phase 11.1 rules, no live call is ever made inside ``pytest``.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from fpl_intelligence.data_providers.base import (
    BaseDataConnector,
    DataParseError,
)
from fpl_intelligence.data_providers.facts import FactSource, PlayerFact

#: Official FPL public endpoints (no key required).
FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FPL_FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"
FPL_ELEMENT_SUMMARY_URL = (
    "https://fantasy.premierleague.com/api/element-summary/{player_id}/"
)

#: FPL ``status`` letter -> normalised availability string.
_FPL_STATUS_MAP: dict[str, str] = {
    "a": "available",
    "d": "doubtful",
    "i": "injured",
    "s": "suspended",
    "u": "unavailable",
    "n": "news",
}


def _to_int(value: Any) -> int | None:
    """Best-effort cast of a JSON value to an int (handles str/None/bool)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _chance_of_playing(element: Mapping[str, Any]) -> int | None:
    """Prefer ``next_round``; fall back to ``this_round``; else ``None``."""
    nxt = _to_int(element.get("chance_of_playing_next_round"))
    if nxt is not None:
        return nxt
    return _to_int(element.get("chance_of_playing_this_round"))


def _normalise_status(element: Mapping[str, Any]) -> str | None:
    raw = element.get("status")
    if not raw:
        return None
    return _FPL_STATUS_MAP.get(str(raw), str(raw))


class FplOfficialConnector(BaseDataConnector):
    """Fetch and normalise core FPL facts from the official public API."""

    name = "fpl_official"

    def __init__(
        self,
        *,
        bootstrap_url: str = FPL_BOOTSTRAP_URL,
        fixtures_url: str = FPL_FIXTURES_URL,
        element_summary_url: str = FPL_ELEMENT_SUMMARY_URL,
        cache: Any = None,
        http_client: Any = None,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
        min_interval_seconds: float = 1.0,
        clock: Any = None,
        monotonic_clock: Any = None,
        sleep: Any = None,
    ) -> None:
        super().__init__(
            cache=cache,
            http_client=http_client,
            timeout=timeout,
            headers=headers,
            min_interval_seconds=min_interval_seconds,
            clock=clock or __import__("time").monotonic,
            monotonic_clock=monotonic_clock or __import__("time").monotonic,
            sleep=sleep or __import__("time").sleep,
        )
        self._bootstrap_url = bootstrap_url
        self._fixtures_url = fixtures_url
        self._element_summary_url = element_summary_url

    # -- network methods -----------------------------------------------------

    def fetch_bootstrap(self) -> dict[str, Any]:
        payload = self._get_json(self._bootstrap_url)
        if not isinstance(payload, dict):
            raise DataParseError("FPL bootstrap payload must be a JSON object")
        return payload

    def fetch_fixtures(self) -> list[dict[str, Any]]:
        payload = self._get_json(self._fixtures_url)
        if not isinstance(payload, list):
            raise DataParseError("FPL fixtures payload must be a JSON array")
        return payload

    def fetch_element_summary(self, player_id: int) -> dict[str, Any]:
        url = self._element_summary_url.format(player_id=player_id)
        payload = self._get_json(url, sensitive=True)
        if not isinstance(payload, dict):
            raise DataParseError("FPL element-summary payload must be a JSON object")
        return payload

    # -- parsing (pure) ------------------------------------------------------

    @staticmethod
    def parse_bootstrap(payload: dict[str, Any]) -> list[PlayerFact]:
        """Project a ``bootstrap-static`` payload into per-player facts."""
        elements = payload.get("elements")
        if not isinstance(elements, list):
            raise DataParseError("FPL bootstrap payload missing 'elements' list")
        teams = {
            int(t["id"]): t
            for t in payload.get("teams", [])
            if isinstance(t, dict) and t.get("id") is not None
        }
        now = datetime.utcnow()
        facts: list[PlayerFact] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            facts.append(FplOfficialConnector._element_to_fact(element, teams, now))
        return facts

    @staticmethod
    def _element_to_fact(
        element: dict[str, Any],
        teams: Mapping[int, dict[str, Any]],
        now: datetime,
    ) -> PlayerFact:
        web = str(element.get("web_name") or "").strip()
        first = str(element.get("first_name") or "").strip()
        second = str(element.get("second_name") or "").strip()
        name = web or f"{first} {second}".strip() or "unknown player"

        team_id = _to_int(element.get("team"))
        team = teams.get(team_id) if team_id is not None else None
        team_name = team.get("name") if isinstance(team, dict) else None

        price_raw = _to_int(element.get("now_cost"))
        price = (price_raw / 10.0) if price_raw is not None else None

        news = str(element.get("news") or "").strip() or None
        return PlayerFact(
            source=FactSource.FPL_OFFICIAL,
            name=name,
            fpl_player_id=_to_int(element.get("id")),
            team_id=team_id,
            team_name=team_name,
            status=_normalise_status(element),
            chance_of_playing=_chance_of_playing(element),
            news=news,
            price=price,
            expected_minutes=_to_int(element.get("expected_minutes")),
            raw=element,
            fetched_at=now,
        )

    @staticmethod
    def parse_fixtures(
        payload: list[dict[str, Any]],
        *,
        team_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Filter fixtures to the next upcoming gameweek for ``team_id``.

        Returns a list of ``{gameweek, team_h, team_a, difficulty}`` dicts
        (``difficulty`` is from ``team_id``'s perspective). Used to annotate a
        player's next fixture difficulty.
        """
        upcoming: list[dict[str, Any]] = []
        for fixture in payload:
            if not isinstance(fixture, dict):
                continue
            event = fixture.get("event")
            if event is None:
                continue  # event is null for fixtures not yet assigned a GW
            if fixture.get("finished"):
                continue
            team_h = _to_int(fixture.get("team_h"))
            team_a = _to_int(fixture.get("team_a"))
            if team_id is not None and team_id not in (team_h, team_a):
                continue
            difficulty = None
            if team_id is not None:
                difficulty = (
                    fixture.get("team_h_difficulty")
                    if team_id == team_h
                    else fixture.get("team_a_difficulty")
                )
            upcoming.append(
                {
                    "gameweek": event,
                    "team_h": team_h,
                    "team_a": team_a,
                    "difficulty": _to_int(difficulty),
                }
            )
        return upcoming

    def next_fixture_difficulty(
        self, fixtures: list[dict[str, Any]], team_id: int | None
    ) -> int | None:
        """Return the difficulty of ``team_id``'s earliest upcoming fixture."""
        relevant = self.parse_fixtures(fixtures, team_id=team_id)
        if not relevant:
            return None
        relevant.sort(key=lambda f: f["gameweek"])
        return relevant[0]["difficulty"]

    # -- convenience ---------------------------------------------------------

    def collect_player_facts(self) -> list[PlayerFact]:
        """Fetch bootstrap + fixtures and return per-player facts.

        Each fact is annotated with its team's next fixture difficulty where
        derivable. This is the method the injector calls.
        """
        bootstrap = self.fetch_bootstrap()
        fixtures = self.fetch_fixtures()
        facts = self.parse_bootstrap(bootstrap)
        by_team: dict[int, int | None] = {}
        for fact in facts:
            if fact.team_id is None:
                continue
            if fact.team_id not in by_team:
                by_team[fact.team_id] = self.next_fixture_difficulty(
                    fixtures, fact.team_id
                )
            fact.fixture_difficulty = by_team[fact.team_id]
        return facts

    def fetch_player_fact(self, player_id: int) -> PlayerFact | None:
        """Fetch the bootstrap and return the single fact for ``player_id``."""
        facts = self.parse_bootstrap(self.fetch_bootstrap())
        for fact in facts:
            if fact.fpl_player_id == player_id:
                return fact
        return None
