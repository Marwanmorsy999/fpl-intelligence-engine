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
    """Players most likely to change price this GW based on net transfer pressure."""
    from sqlalchemy import select  # noqa: PLC0415

    from fpl_intelligence.sync.materialized_models import ElementFactDB  # noqa: PLC0415

    try:
        rows = db.execute(
            select(ElementFactDB).where(ElementFactDB.now_cost.is_not(None))
        ).scalars().all()

        results = []
        for row in rows:
            tin = row.transfers_in_event or 0
            tout = row.transfers_out_event or 0
            net = tin - tout
            if net == 0:
                continue
            price = (row.now_cost or 0) / 10.0
            threshold = 200_000 if price < 5.0 else (350_000 if price < 8.0 else 500_000)
            prob = min(1.0, abs(net) / threshold)
            if prob < 0.25:
                continue
            results.append({
                "element_id": row.element_id,
                "player": row.web_name,
                "price": price,
                "transfers_in_event": tin,
                "transfers_out_event": tout,
                "net_transfers": net,
                "direction": "rise" if net > 0 else "fall",
                "probability": round(prob, 3),
                "status": row.status,
                "chance_of_playing_next_round": row.chance_of_playing_next_round,
            })
        results.sort(key=lambda r: -r["probability"])
        return {
            "predictions": results[:limit],
            "count": len(results[:limit]),
            "note": "probability = |net_transfers| / price_band_threshold",
        }
    except Exception as exc:  # noqa: BLE001
        return {"predictions": [], "count": 0, "note": f"Unavailable: {type(exc).__name__}"}


@router.get("/differentials")
async def differentials(
    db: deps.GetDB,
    max_ownership: float = Query(15.0, ge=0.0, le=50.0),
    limit: int = Query(10, ge=1, le=30),
) -> dict[str, Any]:
    """High-EV low-ownership players by position — differentials worth targeting."""
    from sqlalchemy import select  # noqa: PLC0415

    from fpl_intelligence.sync.materialized_models import (  # noqa: PLC0415
        ElementFactDB,
        PredictionCurrentDB,
    )

    try:
        pred_rows = db.execute(
            select(PredictionCurrentDB)
            .order_by(PredictionCurrentDB.gameweek.desc(), PredictionCurrentDB.expected_points.desc())
        ).scalars().all()

        xpts_map: dict[int, float] = {}
        seen_gw: dict[int, int] = {}
        for pred in pred_rows:
            eid = pred.element_id
            if eid not in xpts_map or pred.gameweek > seen_gw.get(eid, -1):
                xpts_map[eid] = pred.expected_points
                seen_gw[eid] = pred.gameweek

        facts = db.execute(select(ElementFactDB)).scalars().all()
        pos_labels = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
        by_pos: dict[str, list] = {}

        for fact in facts:
            sel = fact.selected_by_percent
            if sel is None:
                continue
            try:
                ownership_f = float(str(sel).replace("%", ""))
            except (TypeError, ValueError):
                continue
            if ownership_f > max_ownership:
                continue
            xpts = xpts_map.get(fact.element_id) or (fact.ep_next or 0.0)
            pos_key = pos_labels.get(fact.element_type or 3, "MID")
            by_pos.setdefault(pos_key, []).append({
                "element_id": fact.element_id,
                "player": fact.web_name or f"Player {fact.element_id}",
                "position": pos_key,
                "ownership_pct": round(ownership_f, 1),
                "xpts": round(float(xpts), 2),
                "price": round(fact.now_cost / 10.0, 1) if fact.now_cost else None,
                "status": fact.status,
                "form": fact.form,
                "chance_of_playing_next_round": fact.chance_of_playing_next_round,
            })

        for pos_key in by_pos:
            by_pos[pos_key].sort(key=lambda r: -r["xpts"])
            by_pos[pos_key] = by_pos[pos_key][:limit]

        return {
            "differentials": by_pos,
            "max_ownership_pct": max_ownership,
            "total": sum(len(v) for v in by_pos.values()),
        }
    except Exception as exc:  # noqa: BLE001
        return {"differentials": {}, "max_ownership_pct": max_ownership, "total": 0,
                "note": f"Unavailable: {type(exc).__name__}"}
