"""Phase 20.0 — fixture difficulty scanning.

Pure, free-data math over the official FPL ``/api/fixtures/`` payload:
per-player next-N-gameweek runs (opponent, home/away, FDR colour), a squad
swing score, and the easiest team runs across the league for transfer
targeting.
"""

from .scanner import (
    FixtureRow,
    PlayerRun,
    TeamRun,
    easiest_team_runs,
    next_gameweeks,
    parse_fixtures,
    player_run,
    squad_swing_score,
    team_short_name,
)

__all__ = [
    "FixtureRow",
    "PlayerRun",
    "TeamRun",
    "easiest_team_runs",
    "next_gameweeks",
    "parse_fixtures",
    "player_run",
    "squad_swing_score",
    "team_short_name",
]
