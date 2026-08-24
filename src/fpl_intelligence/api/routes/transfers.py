"""Phase 25 Gate 0 (T1) — transfer intelligence API.

``GET /api/v1/transfers/ledger?entry_id=`` returns the materialized ledger
with horizon EV per row and the honest source label. ``GET
/api/v1/transfers/detected?session_id=`` powers the on-sync banner when a
snapshot change implies a transfer.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response

from fpl_intelligence.api import deps
from fpl_intelligence.transfers import service as transfer_service

router = APIRouter(prefix="/transfers", tags=["transfers"])


@router.get("/ledger", include_in_schema=False)
async def transfers_ledger(
    response: Response,
    db: deps.GetDB,
    entry_id: str = Query(..., description="FPL entry id."),
) -> dict[str, Any]:
    """Official-first transfer ledger; honest unavailable state otherwise."""
    response.headers["Cache-Control"] = "no-store"
    if not str(entry_id).strip().isdigit():
        return {
            "entry_id": entry_id,
            "status": "unavailable",
            "note": "entry id must be numeric",
            "transfers": [],
            "count": 0,
        }
    return await transfer_service.build_ledger(db, entry_id)


@router.get("/detected", include_in_schema=False)
async def transfers_detected(
    response: Response,
    db: deps.GetDB,
    session_id: str = Query(..., description="FPL entry id (= session key)."),
) -> dict[str, Any]:
    """Latest snapshot-diffed transfer, or an explicit none-detected state."""
    response.headers["Cache-Control"] = "no-store"
    detected = transfer_service.detect_transfer_between_snapshots(db, session_id)
    return {"session_id": session_id, "detected": detected}
