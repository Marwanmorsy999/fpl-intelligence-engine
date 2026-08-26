"""Phase 20.0 — news radar endpoint.

``GET /api/v1/news/radar?session_id=`` matches fresh BBC Sport football
headlines against the saved squad (plus any transfer-plan targets). The feed
is fetched server-side and cached in-process for 6 hours; matching is pure.

Phase 20.1: the primary source is the materialized ``news_cache`` table
(written by the daily 06:10 cron directly from Vercel — BBC RSS is not
blocked). The live fetch only runs as a fallback when the table is cold, and
its result is written back so subsequent requests stay off-network.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.data_providers.bbc_news import (
    NEWS_KEYWORDS,
    NewsItem,
    fetch_items,
    match_headlines,
)
from fpl_intelligence.db.models import Player
from fpl_intelligence.materialize.service import NEWS_MAX_AGE_SECONDS
from fpl_intelligence.squad.service import SquadService
from fpl_intelligence.sync.materialized_models import NewsCacheDB

router = APIRouter(prefix="/news", tags=["news"])
logger = logging.getLogger(__name__)

GetDB = deps.GetDB

#: Phase spec: server-side TTL of six hours for the RSS payload.
NEWS_TTL_SECONDS = 6 * 3600

_feed_lock = threading.Lock()
_feed_cache: tuple[float, list[Any]] | None = None


def _items_from_payload(payload: list[Any]) -> list[NewsItem]:
    """Rebuild NewsItem objects from the cached JSON dicts."""
    out: list[NewsItem] = []
    for item in payload:
        if isinstance(item, dict) and item.get("title"):
            out.append(
                NewsItem(
                    title=str(item["title"]),
                    link=str(item.get("link") or ""),
                    published=str(item.get("published") or ""),
                )
            )
    return out


def cached_items_from_db(
    db: Session, *, max_age_seconds: float = NEWS_MAX_AGE_SECONDS
) -> tuple[list[NewsItem], datetime | None]:
    """Fresh-enough cached headlines plus their fetch time; ``([], None)`` if cold."""
    cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
    row = db.scalar(
        select(NewsCacheDB)
        .where(NewsCacheDB.fetched_at >= cutoff)
        .order_by(NewsCacheDB.id.desc())
    )
    if row is None:
        return [], None
    return _items_from_payload(row.payload or []), row.fetched_at


async def _write_back_news_cache(items: list[NewsItem]) -> None:
    """Persist freshly fetched items into the materialized table (best-effort)."""
    try:
        from fpl_intelligence.db.session import SessionLocal  # noqa: PLC0415

        db = SessionLocal()
        try:
            db.execute(delete(NewsCacheDB))
            db.add(
                NewsCacheDB(
                    source="bbc-rss-live-fallback",
                    headline_count=len(items),
                    payload=[
                        {
                            "title": item.title,
                            "link": item.link,
                            "published": item.published,
                        }
                        for item in items
                    ],
                    fetched_at=datetime.now(UTC),
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — caching is best-effort
        logger.warning("news cache write-back failed: %s", exc)


async def _cached_items() -> list[Any]:
    """Feed items with the 6h TTL; empty list when the feed is unreachable."""
    global _feed_cache
    # Hermetic test suite: no live feed AND no shared SessionLocal under pytest.
    import sys

    if "pytest" in sys.modules or os.getenv("FPL_NO_NETWORK", "") == "1":
        return []

    with _feed_lock:
        if _feed_cache is not None and time.time() - _feed_cache[0] < NEWS_TTL_SECONDS:
            return _feed_cache[1]

    # Phase 20.1 — DB cache next (zero network on the warm path).
    try:
        from fpl_intelligence.db.session import SessionLocal  # noqa: PLC0415

        db = SessionLocal()
        try:
            items, fetched_at = cached_items_from_db(db)
        finally:
            db.close()
        if items and fetched_at is not None:
            # Phase 21.1 fix: fetched_at is a wall-clock UTC timestamp, so the
            # remaining TTL must be measured against the same clock — mixing in
            # time.monotonic() produced garbage ages (the "2864d old" family
            # of clock-domain bugs).
            fetched_utc = (
                fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=UTC)
            )
            age_seconds = (datetime.now(UTC) - fetched_utc).total_seconds()
            with _feed_lock:
                _feed_cache = (time.time() - max(0.0, age_seconds), items)
            return items
    except Exception as exc:  # noqa: BLE001 — fall through to live fetch
        logger.warning("news db-cache read failed: %s", exc)

    try:
        items = await fetch_items()
    except Exception as exc:  # noqa: BLE001 — radar degrades honestly, never 500s
        logger.warning("BBC news feed unavailable: %s", exc)
        return []
    await _write_back_news_cache(items)
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


# ---------------------------------------------------------------------------
# Phase 3.1 — GET /news/bbc-rss
#
# Raw headlines (no squad matching) as a JSON array of
# ``{title, link, pubDate}``. The feed is fetched SERVER-SIDE only (the BBC
# blocks cross-origin browser reads, which is what used to hang the old client
# path); this endpoint reuses :func:`_cached_items` so radar and raw share one
# pipeline (in-memory -> materialized news_cache table -> live fetch with an
# 8s httpx timeout and DB write-back), plus its own 15-minute serialization
# cache so repeated page loads never re-serialize or rate-limit.
# ---------------------------------------------------------------------------

BBC_RSS_TTL_SECONDS = 15 * 60

_rss_lock = threading.Lock()
_rss_cache: tuple[float, list[dict[str, str]]] | None = None


@router.get("/bbc-rss")
async def get_bbc_rss() -> dict[str, Any]:
    """Server-side BBC Sport FPL/football RSS -> JSON array of headlines."""
    global _rss_cache

    now_mono = time.monotonic()
    with _rss_lock:
        if _rss_cache is not None and now_mono - _rss_cache[0] < BBC_RSS_TTL_SECONDS:
            items = _rss_cache[1]
        else:
            items = None

    if items is None:
        try:
            news_items = await _cached_items()
        except Exception as exc:  # noqa: BLE001 — degrade honestly, never 500
            logger.warning("bbc-rss build failed: %s", exc)
            news_items = []
        items = [
            {
                "title": item.title,
                "link": item.link,
                "pubDate": item.published,
            }
            for item in news_items
        ]
        with _rss_lock:
            _rss_cache = (time.monotonic(), items)

    return {"items": items, "count": len(items)}


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
            if (_feed_cache is not None and items and _feed_cache[0] > 0)
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
