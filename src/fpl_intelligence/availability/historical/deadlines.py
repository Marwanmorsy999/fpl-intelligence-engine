"""Load historical FPL gameweek deadlines as PIT materialization cutoffs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.historical.materialize_pit import DeadlineCutoff
from fpl_intelligence.db.models import Gameweek, Season


def list_season_codes(db: Session) -> list[str]:
    """Return all season codes present in the database."""
    return list(db.execute(select(Season.code).order_by(Season.code)).scalars().all())


def _normalize_code(code: str) -> str:
    return code.strip().replace("/", "-")


def resolve_season(db: Session, code: str) -> Season | None:
    """Resolve a season by code, tolerating 2024-25 vs 2024/25."""
    wanted = _normalize_code(code)
    seasons = list(db.execute(select(Season)).scalars().all())
    for s in seasons:
        if _normalize_code(str(s.code)) == wanted:
            return s
    return None


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
        season = resolve_season(db, code)
        if season is None:
            continue
        q = (
            select(Gameweek)
            .where(Gameweek.season_id == season.id)
            .order_by(Gameweek.provider_event_id)
        )
        gws = list(db.execute(q).scalars().all())
        for gw in gws:
            raw_num = gw.provider_event_id
            if raw_num is None:
                continue
            try:
                gw_num = int(raw_num)
            except (TypeError, ValueError):
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
                    season_code=str(season.code),
                    gameweek=gw_num,
                    cutoff=deadline,
                )
            )
            if limit is not None and len(cutoffs) >= limit:
                return cutoffs
    return cutoffs


def diagnose_deadlines(db: Session, seasons: list[str] | None = None) -> dict[str, Any]:
    """Explain why deadline loading may return empty."""
    present = list_season_codes(db)
    target = seasons or present
    detail: dict[str, Any] = {}
    for code in target:
        season = resolve_season(db, code)
        if season is None:
            detail[code] = {"found": False, "reason": "season_code_not_in_db"}
            continue
        gws = list(
            db.execute(select(Gameweek).where(Gameweek.season_id == season.id)).scalars().all()
        )
        with_deadline = sum(1 for g in gws if g.deadline_time is not None)
        detail[str(season.code)] = {
            "found": True,
            "gameweeks": len(gws),
            "with_deadline_time": with_deadline,
            "sample_provider_event_ids": [
                g.provider_event_id for g in gws[:5]
            ],
        }
    return {"seasons_in_db": present, "requested": target, "detail": detail}


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
