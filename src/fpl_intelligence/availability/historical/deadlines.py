"""Load historical gameweek deadlines as point-in-time cutoffs."""
from __future__ import annotations

from datetime import UTC
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.historical.materialize_pit import DeadlineCutoff
from fpl_intelligence.db.models import Gameweek, Season


def list_season_codes(db: Session) -> list[str]:
    return list(db.execute(select(Season.code).order_by(Season.code)).scalars().all())


def _normalize_code(code: str) -> str:
    return code.strip().replace("/", "-")


def resolve_season(db: Session, code: str) -> Season | None:
    wanted = _normalize_code(code)
    for season in db.execute(select(Season)).scalars().all():
        if _normalize_code(str(season.code)) == wanted:
            return season
    return None


def load_deadline_cutoffs(
    db: Session,
    seasons: list[str],
    *,
    gw_min: int | None = None,
    gw_max: int | None = None,
    limit: int | None = None,
) -> list[DeadlineCutoff]:
    """Read only real DB deadlines; missing deadlines are skipped, never guessed."""
    out: list[DeadlineCutoff] = []
    for code in seasons:
        season = resolve_season(db, code)
        if season is None:
            continue
        q = select(Gameweek).where(Gameweek.season_id == season.id).order_by(Gameweek.provider_event_id)
        for gw in db.execute(q).scalars().all():
            raw_num = gw.provider_event_id
            try:
                gw_num = int(raw_num) if raw_num is not None else None
            except (TypeError, ValueError):
                gw_num = None
            if gw_num is None or (gw_min is not None and gw_num < gw_min) or (gw_max is not None and gw_num > gw_max):
                continue
            if gw.deadline_time is None:
                continue
            deadline = gw.deadline_time
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            else:
                deadline = deadline.astimezone(UTC)
            out.append(DeadlineCutoff(str(season.code), gw_num, deadline))
            if limit is not None and len(out) >= limit:
                return out
    return out


def diagnose_deadlines(db: Session, seasons: list[str] | None = None) -> dict[str, Any]:
    present = list_season_codes(db)
    requested = seasons or present
    detail: dict[str, Any] = {}
    for code in requested:
        season = resolve_season(db, code)
        if season is None:
            detail[code] = {"found": False, "reason": "season_code_not_in_db"}
            continue
        gws = list(db.execute(select(Gameweek).where(Gameweek.season_id == season.id)).scalars().all())
        detail[str(season.code)] = {
            "found": True,
            "gameweeks": len(gws),
            "with_deadline_time": sum(g.deadline_time is not None for g in gws),
            "sample_provider_event_ids": [g.provider_event_id for g in gws[:5]],
        }
    return {"seasons_in_db": present, "requested": requested, "detail": detail}


def cutoffs_summary(cutoffs: list[DeadlineCutoff]) -> dict[str, Any]:
    by_season: dict[str, int] = {}
    for cutoff in cutoffs:
        by_season[cutoff.season_code] = by_season.get(cutoff.season_code, 0) + 1
    return {
        "total": len(cutoffs),
        "by_season": by_season,
        "first": cutoffs[0].cutoff.isoformat() if cutoffs else None,
        "last": cutoffs[-1].cutoff.isoformat() if cutoffs else None,
    }
