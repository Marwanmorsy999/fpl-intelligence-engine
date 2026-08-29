"""Load historical FPL gameweek deadlines as PIT materialization cutoffs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.historical.materialize_pit import DeadlineCutoff
from fpl_intelligence.db.models import Gameweek, Season


def load_deadline_cutoffs(
    db: Session,
    seasons: list[str],
    *,
    gw_min: int | None = None,
    gw_max: int | None = None,
    limit: int | None = None,
) -> list[DeadlineCutoff]:
    """Return timezone-aware deadline cutoffs for the requested seasons.

    Gameweeks without ``deadline_time`` are skipped (never fabricated).
    """
    cutoffs: list[DeadlineCutoff] = []
    for code in seasons:
        season = db.scalar(select(Season).where(Season.code == code))
        if season is None:
            continue
        q = (
            select(Gameweek)
            .where(Gameweek.season_id == season.id)
            .order_by(Gameweek.provider_event_id)
        )
        gws = list(db.execute(q).scalars().all())
        for gw in gws:
            gw_num = int(gw.provider_event_id) if gw.provider_event_id is not None else None
            if gw_num is None:
                continue
            if gw_min is not None and gw_num < gw_min:
                continue
            if gw_max is not None and gw_num > gw_max:
                continue
            if gw.deadline_time is None:
                continue
            deadline = gw.deadline_time
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            else:
                deadline = deadline.astimezone(UTC)
            cutoffs.append(
                DeadlineCutoff(
                    season_code=code,
                    gameweek=gw_num,
                    cutoff=deadline,
                )
            )
            if limit is not None and len(cutoffs) >= limit:
                return cutoffs
    return cutoffs


def cutoffs_summary(cutoffs: list[DeadlineCutoff]) -> dict[str, Any]:
    by_season: dict[str, int] = {}
    for c in cutoffs:
        by_season[c.season_code] = by_season.get(c.season_code, 0) + 1
    return {
        "total": len(cutoffs),
        "by_season": by_season,
        "first": cutoffs[0].cutoff.isoformat() if cutoffs else None,
        "last": cutoffs[-1].cutoff.isoformat() if cutoffs else None,
    }
