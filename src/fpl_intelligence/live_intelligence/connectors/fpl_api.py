"""Phase 9.5 — FPL API Connector.

Fetches live fantasy data from the official Fantasy Premier League
``bootstrap-static`` JSON endpoint and turns it into
:class:`~fpl_intelligence.live_intelligence.raw_item_ledger.RawItem` objects.

For each player it surfaces two kinds of ``chance_of_playing`` / ``news``
signals:

* any non-empty ``news`` string (a team's official injury/doubt update), and
* a ``chance_of_playing_*`` below 100 (an availability risk), when ``news`` is
  empty.

Items carry the player's name in the title, the player's FPL element id as the
``external_id``, and the endpoint URL as provenance. Unlike the RSS connector,
the FPL API publishes a snapshot rather than dated articles, so ``published_at``
is the fetch time (we observed it now) — never a fabricated historical time.

No API key is required for ``bootstrap-static``, and none is ever hardcoded
here. Rate limiting (a polite minimum interval) and typed error handling are
inherited from :class:`SourceConnector`.

``--dry-run`` paths / tests inject ``http_client`` (e.g. ``httpx.MockTransport``)
so no live network call is ever made inside ``pytest``.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from datetime import datetime

import httpx

from fpl_intelligence.live_intelligence.connectors.base import (
    SourceConnector,
    SourceParseError,
)
from fpl_intelligence.live_intelligence.rate_limit import (
    MonotonicClock,
    SleepFn,
)
from fpl_intelligence.live_intelligence.raw_item_ledger import RawItem
from fpl_intelligence.live_intelligence.source_registry import SourceType
from fpl_intelligence.live_intelligence.temporal_ledger import Clock, utc_now

#: Official FPL public "static bootstrap" endpoint (no key required).
FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"


def _chance_to_int(value: object) -> int | None:
    """Best-effort cast of a JSON ``chance_of_playing_*`` value to an int."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, float):
        return int(math.trunc(value))
    return None


class FPLAPIConnector(SourceConnector):
    """Fetch player news / availability risk from the official FPL API.

    Args:
        api_url: Override the endpoint URL (for fixtures and tests).
        min_interval_seconds: Polite minimum delay between polls of the
            official API (defaults to 1s — be a good citizen).
        http_client / clock / monotonic_clock / sleep / timeout / headers:
            Injected test seams (see :class:`SourceConnector`).
    """

    name = "fpl_api"
    source_id = "fpl_api_official"
    source_type = SourceType.OFFICIAL_API

    def __init__(
        self,
        *,
        api_url: str = FPL_BOOTSTRAP_URL,
        min_interval_seconds: float = 1.0,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
        http_client: httpx.Client | None = None,
        clock: Clock = utc_now,
        monotonic_clock: MonotonicClock = time.monotonic,
        sleep: SleepFn = time.sleep,
    ) -> None:
        super().__init__(
            http_client=http_client,
            clock=clock,
            monotonic_clock=monotonic_clock,
            sleep=sleep,
            min_interval_seconds=min_interval_seconds,
            timeout=timeout,
            headers=headers,
        )
        self._api_url = api_url

    @property
    def api_url(self) -> str:
        return self._api_url

    def fetch(self, *, limit: int | None = None) -> list[RawItem]:
        response = self._get(self._api_url)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceParseError(
                f"FPL bootstrap payload is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise SourceParseError(
                "FPL bootstrap payload must be a JSON object"
            )

        elements = payload.get("elements")
        if not isinstance(elements, list):
            raise SourceParseError(
                "FPL bootstrap payload missing 'elements' list"
            )

        now: datetime = self._clock()
        items: list[RawItem] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            raw = self._element_to_raw_item(element, now)
            if raw is not None:
                items.append(raw)
            if limit is not None and len(items) >= limit:
                break
        return items

    def _element_to_raw_item(self, element: dict, now: datetime) -> RawItem | None:
        """Project one API ``element`` (a player) into a RawItem, or ``None``."""
        web_name = str(element.get("web_name") or "").strip()
        first = str(element.get("first_name") or "").strip()
        second = str(element.get("second_name") or "").strip()
        name = web_name or f"{first} {second}".strip() or "unknown player"

        news = str(element.get("news") or "").strip()
        chance_next = _chance_to_int(element.get("chance_of_playing_next_round"))
        chance_this = _chance_to_int(element.get("chance_of_playing_this_round"))

        element_id = element.get("id")
        external_id = str(element_id) if element_id is not None else None

        if news:
            content = news
            title = name
        else:
            has_risk = (
                (chance_next is not None and chance_next < 100)
                or (chance_this is not None and chance_this < 100)
            )
            if not has_risk:
                # Fully available ('news' empty, all chances 100/unset): no signal.
                return None
            parts: list[str] = []
            if chance_next is not None:
                parts.append(f"chance_of_playing_next_round={chance_next}%")
            if chance_this is not None:
                parts.append(f"chance_of_playing_this_round={chance_this}%")
            content = f"{name}: {', '.join(parts)}"
            title = f"{name} — availability risk"

        return self._build_raw_item(
            title=title,
            content_text=content,
            published_at=now,
            url=self._api_url,
            external_id=external_id,
        )