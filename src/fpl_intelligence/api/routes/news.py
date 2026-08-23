"""Phase 20.0 — news radar endpoint.

``GET /api/v1/news/radar?session_id=`` matches fresh BBC Sport football
headlines against the saved squad (plus any transfer-plan targets). The feed
is fetched server-side and cached in-process for 6 hours; matching is pure.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.data_providers.bbc_news import (
    NEWS_KEYWORDS,
    fetch_items,
    match_headlines,
)
from fpl_intelligence.db.models import Player
from fpl_intelligence.squad.service import SquadService

router = APIRouter(prefix="/news", tags=["news"])
logger = logging.getLogger(__name__)

GetDB = deps.GetDB

#: Phase spec: server-side TTL of six hours for the RSS payload.
NEWS_TTL_SECONDS = 6 * 3600

_feed_lock = threading.Lock()
_feed_cache: tuple[float, list[Any]] | None = None


async def _cached_items() -> list[Any]:
    """Feed items with the 6h TTL; empty list when the feed is unreachable."""
    global _feed_cache
    with _feed_lock:
        if _feed_cache is not None and time.monotonic() - _feed_cache[0] < NEWS_TTL_SECONDS:
            return _feed_cache[1]
    try:
        items = await fetch_items()
    except Exception as exc:  # noqa: BLE001 — radar degrades honestly, never 500s
        logger.warning("BBC news feed unavailable: %s", exc)
        return []
    with _feed_lock:
        _feed_cache = (time.monotonic(), items)
    return items


def _player_rows(db: Session, pids: list[int]) -> list[tuple[int, str, str, str]]:
    """(player_id, web_name, first_name, second_name) rows for matching."""
    out: list[tuple[int, str, str, str]] = []
    for pid in set(pids):
        row = db.scalar(select(Player).where(Player.fpl_element_id == pid)) or db.get(Player, pid)
        if row is not None:
            out.append((pid, row.web_name, row.first_name, row.second_name))
        else:
            out.append((pid, f"Player {pid}", "", ""))
    return out


@router.get("/radar")
async def news_radar(
    db: GetDB,
    response: Response,
    session_id: str | None = Query(None, description="Per-user session key. Required."),
) -> dict[str, Any]:
    """Match BBC Sport headlines against this squad's players."""
    if not session_id:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    response.headers["Cache-Control"] = "no-store"

    watch_ids = list(squad.player_ids)

    items = await _cached_items()
    flags: dict[str, dict[str, Any]] = {}
    if items:
        flags = match_headlines(items, _player_rows(db, watch_ids), NEWS_KEYWORDS)

    names = _player_rows(db, watch_ids)
    name_map = {str(pid): web for pid, web, _, _ in names}

    return {
        "session_id": session_id,
        "scanned_at": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_feed_cache[0]))
            if (_feed_cache is not None and items)
            else None
        ),
        "headlines_scanned": len(items),
        "matches_found": len(flags),
        "news_flags": [
            {
                "player_id": int(pid),
                "web_name": name_map.get(pid, f"Player {pid}"),
                **flag,
            }
            for pid, flag in flags.items()
        ],
    }
