"""Phase 20.0 — pure fixture-scan math over the FPL fixtures payload.

The official ``GET /api/fixtures/`` array looks like::

    {"event": 8, "team_h": 1, "team_a": 2, "team_h_difficulty": 3,
     "team_a_difficulty": 4, "finished": false, "kickoff_time": "..."}

Everything here is deterministic and unit-testable without network access:
``parse_fixtures`` normalises rows, :func:`next_gameweeks` picks the horizon,
:func:`player_run` projects one club's next-N schedule, and
:func:`squad_swing_score` / :func:`easiest_team_runs` aggregate it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Official FPL team ids (bootstrap-static order) -> 3-letter short names.
TEAM_SHORT_NAMES: dict[int, str] = {
    1: "ARS", 2: "AVL", 3: "BOU", 4: "BRE", 5: "BHA",
    6: "BUR", 7: "CHE", 8: "CRY", 9: "EVE", 10: "FUL",
    11: "LEE", 12: "LIV", 13: "MCI", 14: "MUN", 15: "NEW",
    16: "NFO", 17: "SUN", 18: "TOT", 19: "WHU", 20: "WOL",
}

#: Neutral FDR — the league-average difficulty every swing is measured against.
NEUTRAL_FDR = 3.0

#: Difficulty used when FDR is missing from a row.
NEUTRAL_FDR_INT = 3


def team_short_name(
    team_id: int | None, names: Mapping[int, str] | None = None
) -> str:
    """Short name for an official FPL team id.

    Phase 20.1: prefers a DB-backed ``names`` map (official id -> short name)
    so reshuffled season team ids always render correctly; the static map is
    only a last-resort fallback for unseeded deployments.
    """
    if team_id is None:
        return "?"
    key = int(team_id)
    if names:
        hit = names.get(key)
        if hit:
            return hit
    return TEAM_SHORT_NAMES.get(key, f"T{key}")


@dataclass(frozen=True)
class FixtureRow:
    """Normalised upcoming fixture."""

    event: int
    home_team: int
    away_team: int
    home_difficulty: int
    away_difficulty: int
    finished: bool = False
    kickoff: str | None = None


@dataclass(frozen=True)
class PlayerRun:
    """One player's projected run over a gameweek horizon."""

    gw: int
    opponent_id: int
    opponent: str
    is_home: bool
    difficulty: int


@dataclass(frozen=True)
class TeamRun:
    """One club's easiest-run summary over the horizon."""

    team_id: int
    short_name: str
    avg_fdr: float
    runs: list[PlayerRun] = field(default_factory=list)


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_fixtures(raw: Iterable[Mapping[str, Any]]) -> list[FixtureRow]:
    """Normalise the raw fixtures payload, dropping unassigned/blank rows."""
    rows: list[FixtureRow] = []
    for item in raw:
        event = _to_int(item.get("event"))
        home = _to_int(item.get("team_h"))
        away = _to_int(item.get("team_a"))
        hd = _to_int(item.get("team_h_difficulty"))
        ad = _to_int(item.get("team_a_difficulty"))
        if event is None or home is None or away is None:
            continue
        rows.append(
            FixtureRow(
                event=event,
                home_team=home,
                away_team=away,
                home_difficulty=hd if hd is not None else NEUTRAL_FDR_INT,
                away_difficulty=ad if ad is not None else NEUTRAL_FDR_INT,
                finished=bool(item.get("finished")),
                kickoff=(
                    item.get("kickoff_time")
                    if isinstance(item.get("kickoff_time"), str)
                    else None
                ),
            )
        )
    return rows


def infer_current_gameweek(rows: Sequence[FixtureRow], fallback: int = 1) -> int:
    """First unfinished gameweek in the payload (FPL's own notion of 'now')."""
    pending = sorted({r.event for r in rows if not r.finished})
    if pending:
        return pending[0]
    played = [r.event for r in rows if r.finished]
    return (max(played) + 1) if played else fallback


def next_gameweeks(
    rows: Sequence[FixtureRow], current_gw: int, count: int = 5
) -> list[int]:
    """The next ``count`` gameweeks with any scheduled fixture, from now."""
    events = sorted({r.event for r in rows if r.event >= current_gw})
    return events[: max(count, 0)]


def player_run(
    team_id: int | None,
    rows_by_gw: Mapping[int, Sequence[FixtureRow]],
    horizon: Sequence[int],
    team_names: Mapping[int, str] | None = None,
) -> list[PlayerRun]:
    """Project one club's fixtures across the horizon (one entry per GW)."""
    runs: list[PlayerRun] = []
    for gw in horizon:
        match: FixtureRow | None = None
        for row in rows_by_gw.get(gw, ()):
            if team_id is not None and team_id in (row.home_team, row.away_team):
                match = row
                break
        if match is None or team_id is None:
            runs.append(
                PlayerRun(gw=gw, opponent_id=0, opponent="—",
                          is_home=True, difficulty=NEUTRAL_FDR_INT)
            )
            continue
        is_home = team_id == match.home_team
        opponent = match.away_team if is_home else match.home_team
        difficulty = match.home_difficulty if is_home else match.away_difficulty
        runs.append(
            PlayerRun(
                gw=gw,
                opponent_id=opponent,
                opponent=team_short_name(opponent, team_names),
                is_home=is_home,
                difficulty=int(difficulty),
            )
        )
    return runs


def average_fdr(runs: Sequence[PlayerRun]) -> float:
    """Mean difficulty of a run; neutral when the horizon is empty."""
    real = [r.difficulty for r in runs]
    if not real:
        return NEUTRAL_FDR
    return sum(real) / len(real)


def squad_swing_score(starting_runs: Sequence[float]) -> float:
    """Squad-level fixture swing.

    Each starter contributes ``(NEUTRAL_FDR - own_avg_fdr)`` so positive totals
    mean an easy patch and negative ones a hard patch. Summed over the XI.
    """
    return round(sum(NEUTRAL_FDR - avg for avg in starting_runs), 2)


def easiest_team_runs(
    rows_by_gw: Mapping[int, Sequence[FixtureRow]],
    horizon: Sequence[int],
    top: int = 5,
    exclude_teams: Iterable[int] = (),
    team_names: Mapping[int, str] | None = None,
) -> list[TeamRun]:
    """Rank every club by average FDR over the horizon, easiest first."""
    excluded = set(exclude_teams)
    summaries: list[TeamRun] = []
    seen: set[int] = set()
    for gw_rows in rows_by_gw.values():
        for row in gw_rows:
            seen.add(row.home_team)
            seen.add(row.away_team)
    for team_id in sorted(seen - excluded):
        runs = player_run(team_id, rows_by_gw, horizon, team_names=team_names)
        real = [r for r in runs if r.opponent_id != 0]
        if not real:
            continue
        summaries.append(
            TeamRun(
                team_id=team_id,
                short_name=team_short_name(team_id, team_names),
                avg_fdr=round(average_fdr(real), 2),
                runs=real,
            )
        )
    summaries.sort(key=lambda t: (t.avg_fdr, t.team_id))
    return summaries[: max(top, 0)]
