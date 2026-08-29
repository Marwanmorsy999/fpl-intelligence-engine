"""Season-scoped gameweek resolution for live decision paths.

``Gameweek.provider_event_id`` is unique only within a season
(``uq_gameweek_season_event``). Unscoped ``scalar_one_or_none()`` lookups raise
``MultipleResultsFound`` once historical seasons are ingested — the production
failure behind ``GET /decisions`` 503 responses.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session


def resolve_gameweek_id(db: Session, gameweek: int) -> int | None:
    """Map an FPL event number to the latest season's Gameweek.id."""
    from fpl_intelligence.db.models import Gameweek, Season

    return db.execute(
        select(Gameweek.id)
        .join(Season, Gameweek.season_id == Season.id)
        .where(Gameweek.provider_event_id == int(gameweek))
        .order_by(Season.code.desc())
        .limit(1)
    ).scalar_one_or_none()


def safe_fixture_count(db: Session, player_id: int, gameweek: int) -> int:
    """Return fixture count for a player's team in the current-season gameweek.

    Conservatively returns 1 when the gameweek or membership is unknown.
    """
    from fpl_intelligence.db.models import Fixture, PlayerTeamMembership

    gw_id = resolve_gameweek_id(db, gameweek)
    if gw_id is None:
        return 1

    membership = db.execute(
        select(PlayerTeamMembership.team_id)
        .where(PlayerTeamMembership.player_id == int(player_id))
        .order_by(PlayerTeamMembership.valid_from.desc().nulls_last())
        .limit(1)
    ).scalar_one_or_none()
    if membership is None:
        return 1

    try:
        rows = db.execute(
            select(Fixture.id).where(
                Fixture.gameweek_id == gw_id,
                Fixture.postponed.is_(False),
                (Fixture.home_team_id == membership) | (Fixture.away_team_id == membership),
            )
        ).all()
        count = len(rows)
    except Exception:
        return 1
    return count if count > 0 else 1
