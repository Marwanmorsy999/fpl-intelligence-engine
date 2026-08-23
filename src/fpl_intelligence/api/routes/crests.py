"""Phase 19.0 — team crest endpoint.

TheSportsDB free tier (key ``"3"``) supplies badge artwork; results are cached
in-process for 24h so matchday traffic never hammers the free API. Every
response also carries the real club colors so the UI can render a gradient
avatar fallback without any network round-trip — crests are an enhancement,
never a dependency.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/crests", tags=["crests"])
logger = logging.getLogger(__name__)

#: Official FPL team id -> (name for TheSportsDB lookup, primary hex, accent hex).
CLUB_COLORS: dict[int, tuple[str, str, str]] = {
    1: ("Manchester United", "#da291c", "#fbe122"),
    2: ("Newcastle", "#241f20", "#ffffff"),
    3: ("Bournemouth", "#da291c", "#000000"),
    4: ("Aston Villa", "#95bfe5", "#670e36"),
    5: ("Wolves", "#fdb913", "#231f20"),
    6: ("Everton", "#003399", "#ffffff"),
    7: ("Leicester", "#003090", "#fdp9000"),
    8: ("Arsenal", "#ef0107", "#023474"),
    9: ("West Ham", "#7a263a", "#1bb1e7"),
    10: ("Tottenham", "#132257", "#ffffff"),
    11: ("Brighton", "#0057b8", "#ffcd00"),
    12: ("Liverpool", "#c8102e", "#f6eb61"),
    13: ("Chelsea", "#034694", "#dba111"),
    14: ("Crystal Palace", "#1b458f", "#c4122e"),
    15: ("Man City", "#6cabdd", "#1c2c5b"),
    16: ("Burnley", "#6c1d45", "#99d6ea"),
    17: ("Fulham", "#000000", "#cc0000"),
    18: ("Sunderland", "#eb172b", "#ffffff"),
    19: ("Leeds", "#ffffff", "#1d428a"),
    20: ("Nottingham Forest", "#dd0000", "#ffffff"),
}

#: FPL id -> TheSportsDB team id (stable public ids; verified once, cached here).
_THESPORTSDB_IDS: dict[int, str] = {
    1: "133604",  # Manchester United
    2: "133623",  # Newcastle
    3: "137065",  # Bournemouth
    4: "133592",  # Aston Villa
    5: "133688",  # Wolves
    6: "133601",  # Everton
    7: "133621",  # Leicester (fallback when absent from the PL)
    8: "133603",  # Arsenal
    9: "133599",  # West Ham
    10: "133598",  # Tottenham
    11: "133651",  # Brighton
    12: "133632",  # Liverpool
    13: "133610",  # Chelsea
    14: "133606",  # Crystal Palace
    15: "133613",  # Manchester City
    16: "134778",  # Burnley
    17: "133616",  # Fulham
    18: "133658",  # Sunderland
    19: "134777",  # Leeds
    20: "133662",  # Nottingham Forest
}

_CACHE_TTL_SECONDS = 24 * 3600
_cache: dict[str, tuple[float, str | None]] = {}


@router.get("/{team_id}")
async def crest(team_id: int) -> dict[str, Any]:
    """Badge URL (TheSportsDB, 24h cache) + real club colors for one FPL team."""
    entry = CLUB_COLORS.get(team_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown FPL team id")
    name, color_a, color_b = entry

    key = f"t{team_id}"
    cached_value = _cache.get(key)
    if cached_value is not None and time.monotonic() - cached_value[0] < _CACHE_TTL_SECONDS:
        badge_url: str | None = cached_value[1]
    else:
        badge_url = await _lookup_badge(name, _THESPORTSDB_IDS.get(team_id))
        _cache[key] = (time.monotonic(), badge_url)

    return {
        "team_id": team_id,
        "name": name,
        "badge_url": badge_url,
        "colors": [color_a, color_b],
        "source": "thesportsdb" if badge_url else "club-colors",
    }


async def _lookup_badge(name: str, tsdb_team_id: str | None) -> str | None:
    """Best-effort TheSportsDB badge URL; any failure yields None."""
    try:
        import httpx  # noqa: PLC0415

        url = f"https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t={name}"
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            teams = (r.json() or {}).get("teams") or []
            for team in teams:
                badge = team.get("strBadge") or team.get("strTeamBadge")
                if badge:
                    return str(badge)
        if tsdb_team_id:
            detail_url = f"https://www.thesportsdb.com/api/v1/json/3/lookupteam.php?id={tsdb_team_id}"
            async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
                r = await client.get(detail_url)
                if r.status_code == 200:
                    teams = ((r.json() or {}).get("teams")) or []
                    for team in teams:
                        badge = team.get("strBadge") or team.get("strTeamBadge")
                        if badge:
                            return str(badge)
        return None
    except Exception as exc:  # noqa: BLE001 — crest lookup must never break UI
        logger.info("crest lookup failed for %s: %s", name, exc)
        return None
