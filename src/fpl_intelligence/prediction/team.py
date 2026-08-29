"""Compatibility exports for the canonical temporal team-strength engine."""

from fpl_intelligence.prediction.team_strength_engine import (
    TeamMatch,
    TeamStrengthEngine,
    TeamStrengthEstimate,
)


class TeamStrengthModel(TeamStrengthEngine):
    """Backward-compatible DB facade over :class:`TeamStrengthEngine`."""

    def estimate_all(  # type: ignore[override]
        self,
        db: object,
        cutoff_time: object,
        method: str = "rolling",
        window: int | None = 5,
    ) -> dict[int, TeamStrengthEstimate]:
        return self.estimate_all_from_db(db, cutoff_time, method, window or 5)  # type: ignore[arg-type]


LEAGUE_AVG_GOALS = TeamStrengthEngine.league_average_goals

__all__ = [
    "LEAGUE_AVG_GOALS",
    "TeamMatch",
    "TeamStrengthEngine",
    "TeamStrengthEstimate",
    "TeamStrengthModel",
]
