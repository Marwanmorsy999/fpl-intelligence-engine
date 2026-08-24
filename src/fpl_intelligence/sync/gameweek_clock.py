"""Phase 21.1 (T2) — gameweek auto-advance helpers.

The engine's *target* gameweek must follow the official FPL clock, not a
stored value: at request time we read ``bootstrap-static`` through the egress
mask chain and pick the first event whose deadline is still in the future —
exactly the gameweek the manager's next moves affect. When FPL is unreachable
we degrade to the fixtures-cache inference and then to the saved squad value,
never guessing upward.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: In-process cache for the bootstrap-derived target gameweek. The value moves
#: once per week; ten minutes of staleness costs nothing and keeps the request
#: path off the network almost always.
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
    """True under pytest so network probes never run inside the test suite."""
    import os
    import sys

    return "pytest" in sys.modules or os.environ.get("FPL_NO_NETWORK", "") == "1"


def pick_target_event(events: list[dict[str, Any]], now: datetime | None = None) -> int | None:
    """First event whose deadline has NOT passed yet (pure).

    This is the gameweek transfers/captaincy changes still apply to — FPL's
    own "next deadline" convention. Events without a parseable deadline are
    skipped rather than guessed.
    """
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
    """Target gameweek from live bootstrap-static via the egress masks.

    Cached in-process for :data:`_TARGET_CACHE_SECONDS`. Returns ``None`` when
    bootstrap cannot be reached — callers fall back to fixtures-cache/squad
    values instead of inventing a number. Unit-test environments (pytest) are
    detected and short-circuit to ``None`` so no network is ever attempted.
    """
    if _in_pytest():
        return None

    now_mono = time.monotonic()
    with _target_lock:
        cached_at, cached_value = _target_cache
    if cached_value is not None and now_mono - cached_at < _TARGET_CACHE_SECONDS:
        return cached_value

    try:
        from fpl_intelligence.config import get_settings  # noqa: PLC0415
        from fpl_intelligence.data_providers.fpl_egress import (  # noqa: PLC0415
            FplEgressChain,
            validate_bootstrap_payload,
        )

        cfg = settings or get_settings()
        egress = FplEgressChain(
            cfg.fpl_base_url,
            timeout=cfg.egress_strategy_timeout,
            cache_ttl=cfg.egress_cache_ttl,
        )
        payload = await egress.fetch(
            "/api/bootstrap-static/", validator=validate_bootstrap_payload
        )
    except Exception as exc:  # noqa: BLE001 - honest degradation
        logger.info("bootstrap target gw unavailable: %s", exc)
        return None

    events = payload.get("events") if isinstance(payload, dict) else None
    target = pick_target_event(events if isinstance(events, list) else [])
    if target is not None:
        with _target_lock:
            globals()["_target_cache"] = (time.monotonic(), target)
    return target


async def resolve_target_gameweek(db: Any, fallback: int = 1) -> int:
    """Bootstrap-first target GW with graceful fixtures-cache fallback.

    Order: live bootstrap next-deadline event -> first unfinished gameweek in
    ``fixtures_cache`` -> ``fallback``. Never raises.
    """
    target = await bootstrap_target_gameweek()
    if target is not None:
        return int(target)
    try:
        from sqlalchemy import select  # noqa: PLC0415

        from fpl_intelligence.fixtures.scanner import (  # noqa: PLC0415
            infer_current_gameweek,
            parse_fixtures,
        )
        from fpl_intelligence.sync.materialized_models import FixturesCacheDB  # noqa: PLC0415

        row = db.scalar(select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()).limit(1))
        if row is not None and row.payload:
            return infer_current_gameweek(parse_fixtures(row.payload), fallback=fallback)
    except Exception as exc:  # noqa: BLE001 - never fail a request on metadata
        logger.warning("fixtures-cache gameweek fallback failed: %s", exc)
    return fallback
