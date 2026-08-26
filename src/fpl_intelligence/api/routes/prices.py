"""Phase 23 Gate 1 (L3) — price endpoints (risers/fallers strip + chips)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from fpl_intelligence.api import deps
from fpl_intelligence.prices.service import (
    ensure_price_tables,
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
    """"Today's risers/fallers" top-N cards for the Decisions strip."""
    ensure_price_tables(db)
    return todays_moves_payload(db, limit=limit, gameweek=gameweek)


@router.get("/chips")
async def chips(
    db: deps.GetDB,
    player_ids: str = Query("", description="Comma-separated element ids."),
) -> dict[str, Any]:
    """Latest price delta per requested element — drives the ▲/▼ chips."""
    ensure_price_tables(db)
    wanted = [
        int(p) for p in player_ids.split(",") if p.strip().isdigit()
    ]
    chip_map = price_chip_map(db, wanted)
    return {
        "chips": {str(k): v for k, v in chip_map.items()},
    }
