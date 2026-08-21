"""Phase 11.3 — Player browser endpoint.

A small DX helper so API users can discover valid integer player IDs (with their
team, position, and current price) to feed into ``POST /api/v1/squad``. Players
are read from the ingested database; an optional ``?team=`` query filters by
team ID.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.db.models import (
    Player,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
)

router = APIRouter()

GetDB = deps.GetDB


class PlayerSummary(BaseModel):
    """Compact view of an ingested player for squad-building."""

    id: int
    web_name: str
    team: int | None = None
    position: int | None = None
    price: float | None = None
    #: FPL element code used for Premier-League-CDN photo URLs. May be ``null``
    #: when the player was seeded without a code (falls back to initials avatar).
    code: int | None = None


def _latest_team_id(db: Session, player_id: int) -> int | None:
    membership = db.execute(
        select(PlayerTeamMembership)
        .where(PlayerTeamMembership.player_id == player_id)
        .order_by(PlayerTeamMembership.id.desc())
    ).scalars().first()
    return membership.team_id if membership is not None else None


def _latest_price(db: Session, player_id: int) -> float | None:
    perf = db.execute(
        select(PlayerGameweekPerformance)
        .where(PlayerGameweekPerformance.player_id == player_id)
        .order_by(PlayerGameweekPerformance.gameweek_id.desc())
    ).scalars().first()
    return perf.price if perf is not None else None


@router.get("/players", response_model=list[PlayerSummary])
async def list_players(
    db: GetDB,
    team: int | None = Query(None, description="Optional team ID to filter players by."),
) -> list[PlayerSummary]:
    """List ingested players so callers can find valid IDs for the squad endpoint.

    Returns each player's ``id``, ``web_name``, current ``team`` (team_id),
    ``position`` (position_code: 1=GK, 2=DEF, 3=MID, 4=FWD), and latest ``price``
    in millions. Team and price are best-effort: they are ``null`` when no
    membership / gameweek performance has been ingested for the player.
    """
    query = select(Player)
    if team is not None:
        subq = select(PlayerTeamMembership.player_id).where(
            PlayerTeamMembership.team_id == team
        )
        query = query.where(Player.id.in_(subq))

    players = db.execute(query.order_by(Player.id)).scalars().all()

    return [
        PlayerSummary(
            id=p.id,
            web_name=p.web_name,
            team=_latest_team_id(db, p.id),
            position=p.position_code,
            price=_latest_price(db, p.id),
            code=p.fpl_code,
        )
        for p in players
    ]
