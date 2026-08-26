"""Phase 15.0 — The Odds API connector (free tier, KEY OPTIONAL).

Reads ``THE_ODDS_API_KEY`` from settings. When absent the connector disables
gracefully (``enabled == False``) and every fetch returns ``None`` — market
enrichment is opt-in signal, never a dependency.

Free-tier discipline: 500 credits/month. Each h2h odds request costs 1 credit,
so responses are cached for 12 h and the connector is only consulted once per
gameweek by the prediction chain.

Usage in the engine:

1. **Pre-season proxy signal** — players of the market-favoured team in an
   upcoming fixture get a small, labelled bump (``+0.4 * p_win``).
2. **"Market check" line** on the captain card — "Market agrees" when our
   pick's team is the favourite, "Market disagrees — differential angle"
   otherwise.

Implied-probability conversion: ``p = 1 / decimal_odds``, normalised over the
2-way (h2h) book so probabilities sum to 1.0 (overround removed).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from fpl_intelligence.data_providers.team_aliases import canonical_team_name

logger = logging.getLogger(__name__)

ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
#: Premier League event key on The Odds API.
EPL_SPORT_KEY = "soccer_epl"
#: European bookmakers region (free tier friendly).
REGIONS = "eu"
#: h2h = head-to-head match winner market.
MARKETS = "h2h"
#: 12 h cache — protects the 500 free monthly credits.
ODDS_TTL_SECONDS = 12 * 3600


def implied_probabilities(odds_a: float, odds_b: float) -> tuple[float, float] | None:
    """Convert decimal odds to normalised 2-way implied probabilities.

    >>> implied_probabilities(2.0, 3.0)
    (0.6, 0.4)
    """
    if odds_a <= 1.0 or odds_b <= 1.0:
        return None
    raw_a = 1.0 / odds_a
    raw_b = 1.0 / odds_b
    total = raw_a + raw_b
    if total <= 0:
        return None
    return raw_a / total, raw_b / total


@dataclass
class MatchOdds:
    """Normalised h2h market for one fixture."""

    event_id: str
    commence_time: str
    home_team: str
    away_team: str
    home_win_prob: float
    away_win_prob: float
    bookmakers: int = 0
    #: Best (max) decimal odds per side, kept for transparency.
    home_odds: float = 0.0
    away_odds: float = 0.0

    def favourite(self) -> tuple[str, float]:
        """Return (team_name, prob) of the market favourite."""
        if self.home_win_prob >= self.away_win_prob:
            return self.home_team, self.home_win_prob
        return self.away_team, self.away_win_prob

    def prob_for_team(self, team_name: str) -> float | None:
        """Phase 21.1 (T4): alias-normalised lookup ("MCI" -> Manchester City)."""
        wanted = canonical_team_name(team_name)
        if not wanted:
            return None
        if wanted == canonical_team_name(self.home_team):
            return self.home_win_prob
        if wanted == canonical_team_name(self.away_team):
            return self.away_win_prob
        return None

    def to_payload(self) -> dict[str, Any]:
        fav_team, fav_prob = self.favourite()
        return {
            "event_id": self.event_id,
            "commence_time": self.commence_time,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_win_prob": round(self.home_win_prob, 3),
            "away_win_prob": round(self.away_win_prob, 3),
            "favourite": fav_team,
            "favourite_prob": round(fav_prob, 3),
            "bookmakers": self.bookmakers,
        }


@dataclass
class OddsSnapshot:
    """All parsed EPL h2h markets from one fetch."""

    matches: list[MatchOdds] = field(default_factory=list)
    #: Remaining-credit header from The Odds API (informational).
    requests_remaining: str | None = None

    def for_team(self, team_name: str) -> MatchOdds | None:
        """Alias-normalised event lookup (Phase 21.1 T4)."""
        wanted = canonical_team_name(team_name)
        if not wanted:
            return None
        for match in self.matches:
            if wanted in (
                canonical_team_name(match.home_team),
                canonical_team_name(match.away_team),
            ):
                return match
        return None

    def matched_event_names(self) -> set[str]:
        """Canonical names covered by the current snapshot (for match audits)."""
        names: set[str] = set()
        for match in self.matches:
            names.add(canonical_team_name(match.home_team))
            names.add(canonical_team_name(match.away_team))
        names.discard("")
        return names

    def to_payload(self) -> list[dict[str, Any]]:
        return [m.to_payload() for m in self.matches]


def _best_price(outcomes: list[dict[str, Any]], team: str) -> float:
    """Max decimal odds across bookmakers for one outcome name."""
    best = 0.0
    for outcome in outcomes:
        if str(outcome.get("name", "")).strip().lower() == team.strip().lower():
            try:
                price = float(outcome.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            best = max(best, price)
    return best


def parse_odds_payload(
    payload: list[dict[str, Any]],
) -> list[MatchOdds]:
    """Parse The Odds API h2h events list into normalised markets."""
    matches: list[MatchOdds] = []
    for event in payload:
        if not isinstance(event, dict):
            continue
        home = str(event.get("home_team") or "")
        away = str(event.get("away_team") or "")
        if not home or not away:
            continue
        home_price = 0.0
        away_price = 0.0
        bookmakers = event.get("bookmakers") or []
        for book in bookmakers:
            if not isinstance(book, dict):
                continue
            for market in book.get("markets") or []:
                if market.get("key") != "h2h":
                    continue
                outcomes = market.get("outcomes") or []
                home_price = max(home_price, _best_price(outcomes, home))
                away_price = max(away_price, _best_price(outcomes, away))
        probs = implied_probabilities(home_price, away_price)
        if probs is None:
            continue  # no usable book for this event
        matches.append(
            MatchOdds(
                event_id=str(event.get("id") or ""),
                commence_time=str(event.get("commence_time") or ""),
                home_team=home,
                away_team=away,
                home_win_prob=probs[0],
                away_win_prob=probs[1],
                bookmakers=len(bookmakers),
                home_odds=home_price,
                away_odds=away_price,
            )
        )
    return matches


class OddsApiConnector:
    """The Odds API h2h connector — disabled without a key, cached 12 h."""

    name = "odds_api"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = ODDS_API_BASE_URL,
        cache: Any = None,
        http_client: httpx.Client | None = None,
        timeout: float = 4.0,
        ttl_seconds: int = ODDS_TTL_SECONDS,
        regions: str = REGIONS,
        markets: str = MARKETS,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = base_url.rstrip("/")
        self._cache = cache
        self._client = http_client or httpx.Client(timeout=timeout)
        self._owns_client = http_client is None
        self._ttl = ttl_seconds
        self._regions = regions
        self._markets = markets
        self.enabled = bool(self._api_key)
        if not self.enabled:
            logger.warning(
                "THE_ODDS_API_KEY not set — market-check enrichment disabled "
                "(graceful; set the key to enable)."
            )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_epl_odds(self) -> OddsSnapshot | None:
        """Fetch current EPL h2h odds; None when disabled or on any failure."""
        if not self.enabled:
            return None
        cache_key = f"{self.name}:epl:h2h"
        cached = self._cache.get(cache_key) if self._cache is not None else None
        if isinstance(cached, dict) and isinstance(cached.get("events"), list):
            return OddsSnapshot(
                matches=parse_odds_payload(cached["events"]),
                requests_remaining=cached.get("requests_remaining"),
            )
        try:
            response = self._client.get(
                f"{self._base_url}/sports/{EPL_SPORT_KEY}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": self._regions,
                    "markets": self._markets,
                    "oddsFormat": "decimal",
                },
            )
            response.raise_for_status()
            events = response.json()
            remaining = response.headers.get("x-requests-remaining")
        except Exception as exc:  # noqa: BLE001 - graceful degradation contract
            logger.warning("Odds API fetch failed (disabled gracefully): %s", exc)
            return None
        if not isinstance(events, list):
            return None
        if self._cache is not None and hasattr(self._cache, "set"):
            self._cache.set(
                cache_key,
                {"events": events, "requests_remaining": remaining},
                self._ttl,
            )
        return OddsSnapshot(matches=parse_odds_payload(events), requests_remaining=remaining)
