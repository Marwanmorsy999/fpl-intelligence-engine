"""Gameweek clock helpers with bounded serverless fallback behavior."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)
_TARGET_CACHE_SECONDS = 600.0
_target_cache: tuple[float, int | None] = (0.0, None)
_target_lock = threading.Lock()


def _parse_deadline(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_pytest() -> bool:
    import sys

    return "pytest" in sys.modules or os.environ.get("FPL_NO_NETWORK", "") == "1"


def _serverless_no_live_bootstrap() -> bool:
    """Do not put Vercel requests behind an upstream FPL bootstrap dependency.

    Production already has a daily materialized dataset and fixtures cache. The
    official FPL endpoint intermittently rejects shared cloud egress with 403s;
    retrying several proxy masks here can consume the whole serverless budget.
    """
    return os.environ.get("VERCEL", "") == "1" or os.environ.get("VERCEL_ENV", "") == "production"


def pick_target_event(events: list[dict[str, Any]], now: datetime | None = None) -> int | None:
    moment = now or datetime.now(UTC)
    best: tuple[datetime, int] | None = None
    for event in events or []:
        if not isinstance(event, dict):
            continue
        try:
            event_id = int(event.get("id"))
        except (TypeError, ValueError):
            continue
        deadline = _parse_deadline(event.get("deadline_time"))
        if deadline is None:
            continue
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if deadline <= moment:
            continue
        if best is None or deadline < best[0]:
            best = (deadline, event_id)
    return best[1] if best else None


async def bootstrap_target_gameweek(settings: Any = None) -> int | None:
    """Return the cached/live bootstrap target, but never probe it from Vercel."""
    if _in_pytest() or _serverless_no_live_bootstrap():
        return None

    now_mono = time.monotonic()
    with _target_lock:
        cached_at, cached_value = _target_cache
    if cached_value is not None and now_mono - cached_at < _TARGET_CACHE_SECONDS:
        return cached_value

    try:
        from fpl_intelligence.config import get_settings
        from fpl_intelligence.data_providers.fpl_egress import validate_bootstrap_payload
        from fpl_intelligence.data_providers.registry import get_async_fpl_adapter

        cfg = settings or get_settings()
        payload = await get_async_fpl_adapter(settings=cfg).fetch(
            "/api/bootstrap-static/", validator=validate_bootstrap_payload
        )
    except Exception as exc:  # noqa: BLE001 - metadata must never block callers
        logger.info("bootstrap target gw unavailable: %s", exc)
        return None

    events = payload.get("events") if isinstance(payload, dict) else None
    target = pick_target_event(events if isinstance(events, list) else [])
    if target is not None:
        with _target_lock:
            globals()["_target_cache"] = (time.monotonic(), target)
    return target


async def resolve_target_gameweek(db: Any, fallback: int = 1) -> int:
    """Resolve target GW from safe local cache first on serverless runtimes."""
    target = await bootstrap_target_gameweek()
    if target is not None:
        return int(target)
    try:
        from sqlalchemy import select
        from fpl_intelligence.fixtures.scanner import infer_current_gameweek, parse_fixtures
        from fpl_intelligence.sync.materialized_models import FixturesCacheDB

        row = db.scalar(select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()).limit(1))
        if row is not None and row.payload:
            return infer_current_gameweek(parse_fixtures(row.payload), fallback=fallback)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fixtures-cache gameweek fallback failed: %s", exc)
    return fallback


def resolve_season_gw_ceiling_sync(db: Any, fallback: int | None = None) -> int | None:
    """Resolve a current-season GW ceiling without a serverless network probe."""
    with _target_lock:
        _, cached = _target_cache
    if cached is not None:
        return int(cached)

    if not (_in_pytest() or _serverless_no_live_bootstrap()):
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if not loop.is_running():
                fetched = loop.run_until_complete(bootstrap_target_gameweek())
                if fetched is not None:
                    return int(fetched)
        except Exception as exc:  # noqa: BLE001
            logger.debug("sync bootstrap gw probe failed: %s", exc)

    try:
        from sqlalchemy import select
        from fpl_intelligence.fixtures.scanner import infer_current_gameweek, parse_fixtures
        from fpl_intelligence.sync.materialized_models import FixturesCacheDB

        row = db.scalar(select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()).limit(1))
        if row is not None and row.payload:
            inferred = infer_current_gameweek(
                parse_fixtures(row.payload),
                fallback=fallback if fallback is not None else 0,
            )
            if inferred:
                return int(inferred)
    except Exception as exc:  # noqa: BLE001
        logger.warning("season gw ceiling inference failed: %s", exc)
    return fallback
