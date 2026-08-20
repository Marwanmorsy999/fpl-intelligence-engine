"""Phase 10.4 — Squad Decision Engine REST endpoints.

Phase 11.1 extends ``GET /api/v1/decisions`` so it can *optionally* apply live
structured-API fact overrides (official FPL chance-of-playing, API-Football
confirmed lineups, etc.) before running the Phase 6 optimizers. When live facts
are unavailable — network failure, missing key, or ``live_facts=false`` — the
request falls back to the baseline quantitative predictions and still succeeds.
No API key is hardcoded; live calls (if any) are cache-first and never fail the
request.

Phase 11.2 persists the squad state to PostgreSQL: each request binds a
:class:`~fpl_intelligence.squad.service.SquadService` to the request's database
session, so the squad survives restarts and is shared across workers.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from fpl_intelligence.api import deps
from fpl_intelligence.db.models import Player
from fpl_intelligence.data_providers.decision_bridge import (
    FactCollectionService,
    FactOverrideProvider,
)
from fpl_intelligence.optimization.provider import DecisionPredictionProvider
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
from fpl_intelligence.squad.demo import build_demo_squad
from fpl_intelligence.squad.fpl_import import (
    FplApiUnavailable,
    FplEntryNotFound,
    FplPicksUnavailable,
    FplSquadImporter,
)
from fpl_intelligence.squad.models import (
    DecisionReport,
    FromFplResponse,
    SquadStateCreate,
    SquadStateResponse,
)
from fpl_intelligence.squad.service import SquadService

logger = logging.getLogger(__name__)

router = APIRouter()

GetDB = deps.GetDB


@router.post("/squad", response_model=SquadStateResponse, status_code=200)
async def set_squad(payload: SquadStateCreate, db: GetDB) -> SquadStateResponse:
    """Persist the user's FPL squad state."""
    return SquadService(session=db).set_squad(payload)


@router.get("/squad", response_model=SquadStateResponse | None)
async def get_squad(db: GetDB) -> SquadStateResponse | None:
    """Retrieve the current squad state, or ``null`` if none has been set."""
    return SquadService(session=db).get_squad()


@router.get("/decisions", response_model=DecisionReport)
async def get_decisions(
    db: GetDB,
    provider: Annotated[DecisionPredictionProvider, Depends(deps.get_prediction_provider)],
    live_facts: bool = Query(
        False,
        description="Apply live structured-API fact overrides before optimizing.",
    ),
) -> DecisionReport:
    """Generate a personalized :class:`DecisionReport` for the stored squad.

    When ``live_facts=true`` the engine attempts to fetch hard facts from the
    official FPL API (and any keyed provider that is enabled) and override the
    baseline predictions accordingly. If live facts cannot be obtained the
    request degrades gracefully to the baseline quantitative predictions and
    still succeeds — it never fails because of an upstream API problem.
    """
    squad = SquadService(session=db).get_squad()
    if squad is None:
        raise HTTPException(
            status_code=404,
            detail="No squad configured. POST /api/v1/squad first.",
        )

    applied_overrides: list = []
    if live_facts:
        try:
            result = FactCollectionService().collect_overrides()
            applied_overrides = result.overrides
        except Exception as exc:  # noqa: BLE001 - fall back, never fail the request
            logger.warning(
                "Live fact collection failed; using baseline predictions. %s", exc
            )
            applied_overrides = []

    effective_provider = provider
    if applied_overrides:
        effective_provider = FactOverrideProvider(provider, applied_overrides)

    bridge = DecisionOptimizerBridge(provider=effective_provider)
    report = bridge.generate_decisions(squad)
    report.meta["live_facts_applied"] = len(applied_overrides)
    report.meta["player_positions"] = squad.player_positions or {}
    report.meta["live_fact_sources"] = sorted(
        {o.source.value for o in applied_overrides}
    )
    return report


class FromFplRequest(BaseModel):
    """Request body for the one-click FPL team import."""

    entry_id: int = Field(
        ...,
        gt=0,
        description="Your FPL Manager Entry ID (the number in your FPL team URL).",
        examples=[1234567],
    )


@router.post("/squad/from-fpl", response_model=FromFplResponse, status_code=200)
async def import_squad_from_fpl(
    payload: FromFplRequest, db: GetDB
) -> FromFplResponse:
    """One-click import: resolve an FPL Team ID into a saved squad."""
    importer = FplSquadImporter()
    try:
        result = await importer.build_squad_from_entry(payload.entry_id, db)
    except FplEntryNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Could not find FPL Team ID. Please check your number.",
        ) from exc
    except FplPicksUnavailable as exc:
        # The entry exists but the season hasn't opened: friendly, non-scary.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pre_season",
                "message": str(exc),
                "entry_name": exc.entry_name,
                "gameweek": exc.gameweek,
            },
        ) from exc
    except FplApiUnavailable as exc:
        logger.warning("FPL import failed (API unavailable): %s", exc)
        raise HTTPException(
            status_code=503,
            detail="FPL API is temporarily down, please try again in 5 minutes.",
        ) from exc

    saved = SquadService(session=db).set_squad(result.squad)
    return FromFplResponse(
        squad=saved,
        player_names=result.player_names,
        entry_name=result.entry_name,
        gameweek=result.gameweek,
    )


@router.post("/squad/demo", response_model=FromFplResponse, status_code=200)
async def demo_squad(db: GetDB) -> FromFplResponse:
    """Build a valid demo squad from currently ingested players (no FPL account).

    Picks 2 GK, 5 DEF, 5 MID, 3 FWD from the players already in the database,
    assigns a sensible captain/vice, ~2.0 in the bank and 1 free transfer, then
    persists it via the SquadService so it renders exactly like a real squad.
    """
    from fpl_intelligence.squad.demo import DemoSquadError

    try:
        squad = build_demo_squad(db)
    except DemoSquadError as exc:
        raise HTTPException(
            status_code=503,
            detail="Demo squad unavailable: not enough players ingested yet.",
        ) from exc

    player_names = {
        pid: (db.get(Player, pid).web_name if db.get(Player, pid) else f"Player {pid}")
        for pid in squad.player_ids
    }

    saved = SquadService(session=db).set_squad(squad)
    return FromFplResponse(
        squad=saved,
        player_names=player_names,
        entry_name="Demo Squad",
        gameweek=squad.gameweek,
        is_demo=True,
    )
