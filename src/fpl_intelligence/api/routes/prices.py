"""Phase 23 Gate 1 (L3) — price endpoints (risers/fallers strip + chips)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from fpl_intelligence.api import deps
from fpl_intelligence.prices.service import (
    price_chip_map,
    todays_moves_payload,
)

router = APIRouter(prefix="/prices", tags=["prices"])


@router.get("/moves")
async def moves(
    db: deps.GetDB,
    limit: int = Query(5, ge=1, le=25),
    gameweek: int | None = Query(None),
) -> dict[str, Any]:
    """Today's risers/fallers without request-time schema DDL.

    Price history is enrichment for the dashboard, not a reason to fail the
    entire request. During a transient DB outage return the documented empty
    state so the UI remains usable and callers can retry later.
    """
    try:
        return todays_moves_payload(db, limit=limit, gameweek=gameweek)
    except Exception as exc:  # noqa: BLE001 - honest graceful degradation
        return {
            "risers": [],
            "fallers": [],
            "has_data": False,
            "note": f"Price moves temporarily unavailable: {type(exc).__name__}",
        }


@router.get("/chips")
async def chips(
    db: deps.GetDB,
    player_ids: str = Query("", description="Comma-separated element ids."),
) -> dict[str, Any]:
    """Latest price delta per requested element — drives the ▲/▼ chips."""
    wanted = [int(p) for p in player_ids.split(",") if p.strip().isdigit()]
    try:
        chip_map = price_chip_map(db, wanted)
    except Exception:  # noqa: BLE001 - price chips are optional enrichment
        chip_map = {}
    return {"chips": {str(k): v for k, v in chip_map.items()}}


@router.get("/probability")
async def price_probability(
    db: deps.GetDB,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Players with predicted imminent price changes based on net transfer pressure.

    Uses the transfer-threshold model: probability = min(1, |net_transfers| / threshold)
    where threshold scales by price band (budget/mid/premium).
    Only players with >30% probability are returned, sorted by probability descending.
    """
    from sqlalchemy import select

    from fpl_intelligence.db.models import PerformanceDB
    from fpl_intelligence.tools.price_predictor import predict_price_changes

    try:
        stmt = (
            select(PerformanceDB)
            .order_by(PerformanceDB.id.desc())
            .limit(2000)
        )
        rows = db.execute(stmt).scalars().all()
        predictions = predict_price_changes(rows, limit=limit)
        return {
            "predictions": predictions,
            "count": len(predictions),
            "note": "probability = net_transfers / threshold (capped at 100%)",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "predictions": [],
            "count": 0,
            "note": f"Price probability temporarily unavailable: {type(exc).__name__}",
        }


@router.get("/differentials")
async def differentials(
    db: deps.GetDB,
    max_ownership: float = Query(15.0, ge=0.0, le=50.0, description="Max ownership % to qualify as differential"),
    limit: int = Query(10, ge=1, le=30),
) -> dict[str, Any]:
    """High-EV players with low ownership — differentials worth targeting.

    Returns players below the ownership threshold sorted by xPTS descending,
    split by position so managers can find differentials in each slot.
    """
    from sqlalchemy import select

    from fpl_intelligence.db.models import PerformanceDB

    try:
        stmt = (
            select(PerformanceDB)
            .order_by(PerformanceDB.id.desc())
            .limit(5000)
        )
        rows = db.execute(stmt).scalars().all()

        # Deduplicate to latest row per player
        seen: set[int] = set()
        players = []
        for row in rows:
            pid = getattr(row, "element_id", None) or getattr(row, "player_id", None)
            if pid and pid not in seen:
                seen.add(pid)
                players.append(row)

        results: list[dict[str, Any]] = []
        for row in players:
            ownership = getattr(row, "selected_by_percent", None)
            xpts = getattr(row, "expected_points", None) or getattr(row, "ep_next", None)
            if ownership is None or xpts is None:
                continue
            try:
                ownership_f = float(str(ownership).replace("%", ""))
                xpts_f = float(xpts)
            except (TypeError, ValueError):
                continue
            if ownership_f > max_ownership:
                continue
            pid = getattr(row, "element_id", None) or getattr(row, "player_id", None)
            name = getattr(row, "web_name", None) or f"Player {pid}"
            pos = getattr(row, "element_type", None) or getattr(row, "position_code", None)
            price = getattr(row, "now_cost", None)
            results.append({
                "player_id": pid,
                "player": name,
                "position": pos,
                "ownership_pct": round(ownership_f, 1),
                "xpts": round(xpts_f, 2),
                "price": round(float(price) / 10, 1) if price else None,
            })

        results.sort(key=lambda r: -r["xpts"])
        by_pos: dict[str, list] = {}
        for r in results:
            pos_key = str(r.get("position", "UNK"))
            by_pos.setdefault(pos_key, []).append(r)

        # Limit per position
        for pos_key in by_pos:
            by_pos[pos_key] = by_pos[pos_key][:limit]

        return {
            "differentials": by_pos,
            "max_ownership_pct": max_ownership,
            "total": sum(len(v) for v in by_pos.values()),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "differentials": {},
            "max_ownership_pct": max_ownership,
            "total": 0,
            "note": f"Differentials temporarily unavailable: {type(exc).__name__}",
        }
