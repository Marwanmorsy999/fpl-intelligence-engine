"""Phase 10.4 — Squad Decision Engine REST endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from fpl_intelligence.api import deps
from fpl_intelligence.optimization.provider import DecisionPredictionProvider
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
from fpl_intelligence.squad.models import (
    DecisionReport,
    SquadStateCreate,
    SquadStateResponse,
)
from fpl_intelligence.squad.service import SquadService

router = APIRouter()

_squad_service = SquadService()


@router.post("/squad", response_model=SquadStateResponse, status_code=200)
async def set_squad(payload: SquadStateCreate) -> SquadStateResponse:
    """Persist the user's FPL squad state."""
    return _squad_service.set_squad(payload)


@router.get("/squad", response_model=SquadStateResponse | None)
async def get_squad() -> SquadStateResponse | None:
    """Retrieve the current squad state, or ``null`` if none has been set."""
    return _squad_service.get_squad()


@router.get("/decisions", response_model=DecisionReport)
async def get_decisions(
    provider: Annotated[DecisionPredictionProvider, Depends(deps.get_prediction_provider)],
) -> DecisionReport:
    """Generate a personalized :class:`DecisionReport` for the stored squad."""
    squad = _squad_service.get_squad()
    if squad is None:
        raise HTTPException(
            status_code=404,
            detail="No squad configured. POST /api/v1/squad first.",
        )
    bridge = DecisionOptimizerBridge(provider=provider)
    return bridge.generate_decisions(squad)
