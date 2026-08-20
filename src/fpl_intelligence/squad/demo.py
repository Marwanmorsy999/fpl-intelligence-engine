"""Phase 13.1 - Demo squad builder.

Builds a valid, renderable 15-player squad (2 GK, 5 DEF, 5 MID, 3 FWD) from the
players already ingested into the local database. Using real DB players means
their display names always resolve in the dashboard, and we attach prices (real
when available, otherwise a deterministic placeholder) so the squad renders
exactly like a real import.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import Player, PlayerGameweekPerformance
from fpl_intelligence.squad.models import SquadStateCreate

#: Required formation by ``position_code`` (1=GK, 2=DEF, 3=MID, 4=FWD).
_DEMO_FORMATION: dict[int, int] = {1: 2, 2: 5, 3: 5, 4: 3}

_DEFAULT_CHIPS = ["wildcard", "free_hit", "bench_boost", "triple_captain"]

#: Pre-season demo points at the upcoming Gameweek 1.
_DEMO_GAMEWEEK = 1


class DemoSquadError(Exception):
    """Raised when the database does not contain enough players for a demo."""


def _synthetic_price(position_code: int, seed: int) -> float:
    """Deterministic placeholder price (in millions) per position."""
    base = {1: 4.0, 2: 4.0, 3: 5.0, 4: 5.5}.get(position_code, 4.5)
    return round(base + (seed % 6) * 0.5, 1)


def _price_for_player(db: Session, player: Player, seed: int) -> float:
    """Use the latest ingested price, else a deterministic placeholder."""
    price = db.execute(
        select(PlayerGameweekPerformance.price)
        .where(PlayerGameweekPerformance.player_id == player.id)
        .order_by(PlayerGameweekPerformance.gameweek_id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if price is not None and price > 0:
        return float(price)
    return _synthetic_price(player.position_code or 3, seed)


def build_demo_squad(db: Session) -> SquadStateCreate:
    """Construct a valid demo :class:`SquadStateCreate` from ingested players."""
    players = db.execute(select(Player)).scalars().all()
    by_position: dict[int, list[Player]] = {1: [], 2: [], 3: [], 4: []}
    for player in players:
        code = player.position_code or 3
        if code in by_position:
            by_position[code].append(player)

    chosen: list[Player] = []
    for code, needed in _DEMO_FORMATION.items():
        pool = sorted(by_position[code], key=lambda p: p.id)
        if len(pool) < needed:
            raise DemoSquadError(
                f"Not enough ingested players for demo squad: need {needed} "
                f"players in position {code}, found {len(pool)}."
            )
        chosen.extend(pool[:needed])

    player_ids = [p.id for p in chosen]
    player_positions = {p.id: (p.position_code or 3) for p in chosen}
    player_prices: dict[int, float] = {}
    for idx, p in enumerate(chosen):
        player_prices[p.id] = _price_for_player(db, p, idx)
    player_teams = {p.id: (p.id % 20) + 1 for p in chosen}

    # Captain / vice: highest-priced outfield (MID/FWD) players.
    outfield = [p for p in chosen if player_positions[p.id] in (3, 4)]
    outfield.sort(key=lambda p: player_prices[p.id], reverse=True)
    captain = outfield[0]
    vice = outfield[1] if len(outfield) > 1 else chosen[0]

    return SquadStateCreate(
        player_ids=player_ids,
        captain_id=captain.id,
        vice_captain_id=vice.id,
        bank=2.0,
        free_transfers=1,
        chips_available=list(_DEFAULT_CHIPS),
        gameweek=_DEMO_GAMEWEEK,
        player_positions=player_positions,
        player_prices=player_prices,
        player_teams=player_teams,
        is_demo=True,
    )
