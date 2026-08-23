"""Phase 20.1 — vaastav raw.githubusercontent fetchers and parsers.

vaastav mirrors the official FPL data as CSV on GitHub, which is reachable
from Vercel's datacenter IPs even when fantasy.premierleague.com itself is
blocked. Every function here is pure (fetch vs parse split) so parsers are
unit-testable against captured payloads.

Layout consumed:

* ``data/{season}/gw/{n}.csv``        — per-element gameweek results
* ``data/{season}/fixtures.csv``      — full season fixture list with FDR
* ``data/{season}/players_raw.csv``   — bootstrap-style element facts

All fetchers tolerate 404 (a GW not yet published) by returning ``None``.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

VAASTAV_RAW_BASE = (
    "https://raw.githubusercontent.com/vaastav/"
    "Fantasy-Premier-League/master/data"
)

#: Generous but bounded: players_raw.csv is ~600KB of CSV.
_FETCH_TIMEOUT_SECONDS = 20.0


def gw_url(season_code: str, gameweek: int) -> str:
    """URL of one gameweek's results CSV."""
    return f"{VAASTAV_RAW_BASE}/{season_code}/gw/{int(gameweek)}.csv"


def fixtures_url(season_code: str) -> str:
    return f"{VAASTAV_RAW_BASE}/{season_code}/fixtures.csv"


def players_raw_url(season_code: str) -> str:
    return f"{VAASTAV_RAW_BASE}/{season_code}/players_raw.csv"


async def fetch_text(url: str) -> str | None:
    """Fetch a text payload; ``None`` on 404/empty so callers can skip."""
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": "fpl-intelligence-engine/20.1"},
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
    except Exception as exc:  # noqa: BLE001 — cron degrades gracefully
        logger.warning("materialize fetch failed %s: %s", url, exc)
        return None
    if response.status_code == 404:
        return None
    if response.status_code != 200 or not response.text.strip():
        logger.warning("materialize fetch unexpected %s for %s", response.status_code, url)
        return None
    return response.text


def _rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _opt_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_gw_results_csv(text: str) -> list[dict[str, Any]]:
    """Parse one GW results CSV into normalized per-element rows.

    Output rows carry the columns persisted into ``ingested_history``:
    ``element_id`` / ``total_points`` / ``minutes`` / ``bonus`` /
    ``goals_scored`` / ``assists`` plus the untouched ``payload`` dict.
    """
    out: list[dict[str, Any]] = []
    for row in _rows(text):
        element_id = _opt_int(row.get("element"))
        if element_id is None:
            continue
        out.append(
            {
                "element_id": element_id,
                "total_points": _opt_int(row.get("total_points")) or 0,
                "minutes": _opt_int(row.get("minutes")),
                "bonus": _opt_int(row.get("bonus")),
                "goals_scored": _opt_int(row.get("goals_scored")),
                "assists": _opt_int(row.get("assists")),
                "payload": row,
            }
        )
    return out


def parse_fixtures_csv(text: str) -> list[dict[str, Any]]:
    """Parse fixtures.csv into official-API-shaped dicts.

    The scanner consumes exactly the keys of the official ``/api/fixtures/``
    payload it needs: event/team_h/team_a/difficulties/finished/kickoff_time —
    so downstream code needs zero changes versus live-FPL ingestion.
    """
    out: list[dict[str, Any]] = []
    for row in _rows(text):
        event = _opt_int(row.get("event"))
        team_h = _opt_int(row.get("team_h"))
        team_a = _opt_int(row.get("team_a"))
        if event is None or team_h is None or team_a is None:
            continue
        out.append(
            {
                "event": event,
                "team_h": team_h,
                "team_a": team_a,
                "team_h_difficulty": _opt_int(row.get("team_h_difficulty")),
                "team_a_difficulty": _opt_int(row.get("team_a_difficulty")),
                "finished": str(row.get("finished", "")).lower() == "true",
                "kickoff_time": row.get("kickoff_time") or None,
            }
        )
    return out


def parse_players_raw_csv(text: str) -> dict[int, dict[str, Any]]:
    """Parse players_raw.csv into ``{element_id: fact-dict}`` snapshots."""
    facts: dict[int, dict[str, Any]] = {}
    for row in _rows(text):
        element_id = _opt_int(row.get("id"))
        if element_id is None:
            continue
        facts[element_id] = {
            "web_name": (row.get("web_name") or "").strip() or None,
            "team_id": _opt_int(row.get("team")),
            "minutes": _opt_int(row.get("minutes")),
            "selected_by_percent": (row.get("selected_by_percent") or "").strip() or None,
            "cost_change_event": _opt_int(row.get("cost_change_event")),
            "status": (row.get("status") or "").strip() or None,
            "news": (row.get("news") or "").strip()[:500] or None,
        }
    return facts
