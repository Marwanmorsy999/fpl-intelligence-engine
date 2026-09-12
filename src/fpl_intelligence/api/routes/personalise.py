"""Personalisation endpoints — risk profile, FPL-compatible picks, injury watch."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from fpl_intelligence.api import deps

router = APIRouter(prefix="/api/v1/personalise", tags=["personalise"])


@router.get("/picks")
async def fpl_picks(
    session_id: str = Query(...),
    db: deps.GetDB = deps.GetDB,
) -> dict[str, Any]:
    """Squad in FPL picks[] format (mirrors /api/entry/{id}/event/{gw}/picks/)."""
    from fpl_intelligence.squad.models_db import SquadStateDB  # noqa: PLC0415

    row = db.execute(
        select(SquadStateDB)
        .where(SquadStateDB.session_id == session_id)
        .order_by(SquadStateDB.updated_at.desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        return {"picks": [], "note": "No squad found for this session"}

    player_ids: list[int] = row.player_ids or []
    captain_id: int | None = row.captain_id
    vice_captain_id: int | None = row.vice_captain_id

    picks = [
        {
            "element": pid,
            "position": idx + 1,
            "multiplier": 2 if pid == captain_id else 1,
            "is_captain": pid == captain_id,
            "is_vice_captain": pid == vice_captain_id,
        }
        for idx, pid in enumerate(player_ids)
    ]
    return {
        "picks": picks,
        "active_chip": None,
        "automatic_subs": [],
        "entry_history": {
            "bank": getattr(row, "bank", None),
            "event_transfers": None,
            "event_transfers_cost": None,
        },
    }


_PROFILES: dict[str, dict[str, Any]] = {
    "aggressive": {
        "label": "Aggressive",
        "description": "Chasing rank — take hits, back differentials, activate chips early",
        "transfer_hit_threshold": 0.40,
        "captain_differential_bonus": 0.15,
        "chip_activation_multiplier": 0.85,
    },
    "balanced": {
        "label": "Balanced",
        "description": "Stable — take hits only with strong justification",
        "transfer_hit_threshold": 0.55,
        "captain_differential_bonus": 0.05,
        "chip_activation_multiplier": 1.00,
    },
    "safe": {
        "label": "Safe",
        "description": "Protecting rank — avoid hits, template captains, late chip activation",
        "transfer_hit_threshold": 0.70,
        "captain_differential_bonus": -0.05,
        "chip_activation_multiplier": 1.20,
    },
}


@router.get("/risk-profile")
async def get_risk_profile(
    session_id: str = Query(...),
    db: deps.GetDB = deps.GetDB,
) -> dict[str, Any]:
    """Return risk profile config for this session."""
    from fpl_intelligence.squad.models_db import SquadStateDB  # noqa: PLC0415

    row = db.execute(
        select(SquadStateDB)
        .where(SquadStateDB.session_id == session_id)
        .order_by(SquadStateDB.updated_at.desc())
        .limit(1)
    ).scalars().first()

    profile = "balanced"
    if row and getattr(row, "risk_profile", None):
        profile = row.risk_profile

    return {
        "profile": profile,
        "config": _PROFILES.get(profile, _PROFILES["balanced"]),
        "all_profiles": list(_PROFILES.keys()),
    }


@router.get("/injury-watch")
async def injury_watch(
    session_id: str | None = Query(None),
    db: deps.GetDB = deps.GetDB,
) -> dict[str, Any]:
    """Players with non-available status and injury news; squad players surfaced first."""
    from fpl_intelligence.squad.models_db import SquadStateDB  # noqa: PLC0415
    from fpl_intelligence.sync.materialized_models import ElementFactDB  # noqa: PLC0415

    squad_pids: set[int] = set()
    if session_id:
        row = db.execute(
            select(SquadStateDB)
            .where(SquadStateDB.session_id == session_id)
            .order_by(SquadStateDB.updated_at.desc())
            .limit(1)
        ).scalars().first()
        if row and row.player_ids:
            squad_pids = set(row.player_ids)

    facts = db.execute(
        select(ElementFactDB).where(ElementFactDB.status.notin_(["a", None]))
    ).scalars().all()

    pos_labels = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    status_labels = {"d": "Doubt", "i": "Injured", "s": "Suspended", "u": "Unavailable", "n": "Not in squad"}
    severity = {"i": 0, "s": 1, "d": 2, "u": 3, "n": 4}

    alerts = [
        {
            "element_id": f.element_id,
            "player": f.web_name,
            "position": pos_labels.get(f.element_type or 3, "UNK"),
            "team_id": f.team_id,
            "status": f.status,
            "status_label": status_labels.get(f.status, f.status),
            "chance_of_playing_next_round": f.chance_of_playing_next_round,
            "chance_of_playing_this_round": f.chance_of_playing_this_round,
            "news": f.news or "",
            "price": round(f.now_cost / 10.0, 1) if f.now_cost else None,
            "in_your_squad": f.element_id in squad_pids,
        }
        for f in facts if f.news or f.status not in ("u", None)
    ]
    alerts.sort(key=lambda a: (0 if a["in_your_squad"] else 1, severity.get(a["status"], 9)))

    return {
        "alerts": alerts,
        "total": len(alerts),
        "in_squad": sum(1 for a in alerts if a["in_your_squad"]),
    }


@router.get("/price-countdown")
async def price_countdown() -> dict[str, Any]:
    """Hours and minutes until next FPL price update (~1am UK time)."""
    now = datetime.now(UTC)
    is_bst = 3 <= now.month <= 10
    target_utc_hour = 0 if is_bst else 1  # 1am UK
    target = now.replace(hour=target_utc_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    diff_s = int((target - now).total_seconds())
    h, m = diff_s // 3600, (diff_s % 3600) // 60
    return {
        "hours": h,
        "minutes": m,
        "label": f"{h}h {m}m" if h > 0 else f"{m}m",
        "imminent": h == 0 and m < 30,
    }
