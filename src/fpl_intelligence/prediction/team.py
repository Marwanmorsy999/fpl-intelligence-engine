"""Team strength model.

Estimates, as of a historical cutoff T using ONLY pre-match data:

- ``attack_strength``: attacking strength (goals/xG scored relative to league avg)
- ``defence_strength``: defensive strength (goals/xGA conceded relative to league avg)
- ``home_strength``: expected goals scored at home
- ``away_strength``: expected goals scored away

Both rolling and season-to-date estimates are supported. The model never
uses final season statistics for earlier predictions.

Values are kept interpretable: a strength of 1.0 means league average,
1.2 means 20% above average, 0.8 means 20% below average.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import TeamMatchPerformance
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
    apply_policy,
)
from fpl_intelligence.prediction.models import TeamStrengthRecord

LEAGUE_AVG_GOALS = 1.4  # Premier League historical average goals per team per match


@dataclass
class TeamStrengthEstimate:
    """An interpretable team-strength estimate as of a cutoff.

    Attributes:
        team_id: Team ID.
        cutoff_time: The decision cutoff.
        attack_strength: Attack strength (1.0 = league average).
        defence_strength: Defence strength (1.0 = league average).
        home_strength: Expected home goals.
        away_strength: Expected away goals.
        sample_size: Number of source matches.
        completeness: 0-1 data-completeness.
        method: ``rolling`` or ``season_to_date``.
        window: Rolling window size (if rolling).
    """

    team_id: int
    cutoff_time: datetime
    attack_strength: float
    defence_strength: float
    home_strength: float
    away_strength: float
    sample_size: int
    completeness: float
    method: str
    window: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "cutoff_time": self.cutoff_time.isoformat(),
            "attack_strength": self.attack_strength,
            "defence_strength": self.defence_strength,
            "home_strength": self.home_strength,
            "away_strength": self.away_strength,
            "sample_size": self.sample_size,
            "completeness": self.completeness,
            "method": self.method,
            "window": self.window,
        }


class TeamStrengthModel:
    """Estimates team attack/defence/home/away strength as of a cutoff.

    The model reads pre-cutoff ``TeamMatchPerformance`` records directly via
    the provided session. It does not own the session; callers supply it.
    """

    def __init__(
        self,
        feature_version: str = "1.0.0",
        policy: InformationAccessPolicy = DEFAULT_POLICY,
        league_avg_goals: float = LEAGUE_AVG_GOALS,
    ) -> None:
        self._feature_version = feature_version
        self._policy = policy
        self._league_avg_goals = league_avg_goals

    @property
    def model_name(self) -> str:
        return "team_strength_model"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    # ------------------------------------------------------------------
    # Estimation
    # ------------------------------------------------------------------

    def estimate_team(
        self,
        db: Session,
        team_id: int,
        cutoff_time: datetime,
        method: str = "rolling",
        window: int | None = 5,
    ) -> TeamStrengthEstimate:
        """Estimate strength for a single team as of a cutoff.

        Args:
            db: Database session.
            team_id: Team ID.
            cutoff_time: The decision cutoff (pre-match data only).
            method: ``rolling`` (last N matches) or ``season_to_date``.
            window: Rolling window size (default 5).

        Returns:
            A ``TeamStrengthEstimate``.
        """
        perfs = self._load_performances(db, team_id, cutoff_time)
        if not perfs:
            return TeamStrengthEstimate(
                team_id=team_id,
                cutoff_time=cutoff_time,
                attack_strength=1.0,
                defence_strength=1.0,
                home_strength=self._league_avg_goals,
                away_strength=self._league_avg_goals,
                sample_size=0,
                completeness=0.0,
                method=method,
                window=window,
            )

        # Filter by window.
        if method == "rolling" and window is not None and len(perfs) > window:
            perfs = perfs[-window:]

        n = len(perfs)
        goals_scored = [p.goals_scored or 0 for p in perfs]
        goals_conceded = [p.goals_conceded or 0 for p in perfs]
        xg = [
            p.expected_goals if p.expected_goals is not None else p.goals_scored or 0 for p in perfs
        ]
        xga = [
            (
                p.expected_goals_conceded
                if p.expected_goals_conceded is not None
                else p.goals_conceded or 0
            )
            for p in perfs
        ]

        avg_goals = sum(goals_scored) / n
        avg_conceded = sum(goals_conceded) / n
        avg_xg = sum(xg) / n
        avg_xga = sum(xga) / n

        # Blend goals and xG (if available) for a smoother strength estimate.
        # xG is only used when at least half the records have non-null xG.
        xg_count = sum(1 for p in perfs if p.expected_goals is not None)
        if xg_count >= n / 2:
            attack_rate = 0.5 * avg_goals + 0.5 * avg_xg
            defence_rate = 0.5 * avg_conceded + 0.5 * avg_xga
        else:
            attack_rate = avg_goals
            defence_rate = avg_conceded

        attack_strength = attack_rate / self._league_avg_goals
        # Defence strength: lower conceded is stronger.
        defence_strength = self._league_avg_goals / defence_rate if defence_rate > 0 else 2.0
        # Cap to a sensible range to preserve interpretability.
        attack_strength = float(min(2.5, max(0.2, attack_strength)))
        defence_strength = float(min(2.5, max(0.2, defence_strength)))

        # Home/away expected goals.
        home = [p.goals_scored or 0 for p in perfs if p.is_home]
        away = [p.goals_scored or 0 for p in perfs if not p.is_home]
        home_strength = sum(home) / len(home) if home else self._league_avg_goals
        away_strength = sum(away) / len(away) if away else self._league_avg_goals

        completeness = min(1.0, n / 10.0)

        return TeamStrengthEstimate(
            team_id=team_id,
            cutoff_time=cutoff_time,
            attack_strength=attack_strength,
            defence_strength=defence_strength,
            home_strength=home_strength,
            away_strength=away_strength,
            sample_size=n,
            completeness=completeness,
            method=method,
            window=window,
        )

    def estimate_all(
        self,
        db: Session,
        cutoff_time: datetime,
        method: str = "rolling",
        window: int | None = 5,
    ) -> dict[int, TeamStrengthEstimate]:
        """Estimate strength for every team with pre-cutoff data.

        Args:
            db: Database session.
            cutoff_time: The decision cutoff.
            method: ``rolling`` or ``season_to_date``.
            window: Rolling window size.

        Returns:
            Dict mapping team_id -> TeamStrengthEstimate.
        """
        team_ids = self._get_team_ids(db, cutoff_time)
        return {tid: self.estimate_team(db, tid, cutoff_time, method, window) for tid in team_ids}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def persist(
        self,
        db: Session,
        estimate: TeamStrengthEstimate,
        season: str,
        feature_version: str | None = None,
    ) -> TeamStrengthRecord:
        """Persist a strength estimate as an immutable record."""
        record = TeamStrengthRecord(
            team_id=estimate.team_id,
            season=season,
            cutoff_time=estimate.cutoff_time,
            feature_version=feature_version or self._feature_version,
            attack_strength=estimate.attack_strength,
            defence_strength=estimate.defence_strength,
            home_strength=estimate.home_strength,
            away_strength=estimate.away_strength,
            sample_size=estimate.sample_size,
            completeness=estimate.completeness,
        )
        db.add(record)
        db.flush()
        return record

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_performances(
        self, db: Session, team_id: int, cutoff_time: datetime
    ) -> list[TeamMatchPerformance]:
        stmt = select(TeamMatchPerformance).where(
            TeamMatchPerformance.team_id == team_id,
        )
        try:
            condition = apply_policy(TeamMatchPerformance, self._policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass
        stmt = stmt.order_by(TeamMatchPerformance.fixture_id)
        return list(db.execute(stmt).scalars().all())

    def _get_team_ids(self, db: Session, cutoff_time: datetime) -> list[int]:
        stmt = select(TeamMatchPerformance.team_id).distinct()
        try:
            condition = apply_policy(TeamMatchPerformance, self._policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass
        return list(db.execute(stmt).scalars().all())

    def estimate_from_features(self, features: dict[str, float]) -> dict[str, float]:
        """Estimate strength from a pre-computed feature dict (for testing).

        This convenience method allows the match model and player pipeline
        to consume team strength without a DB round trip when features are
        already available.
        """
        return {
            "attack_strength": features.get("attack_strength", 1.0),
            "defence_strength": features.get("defensive_strength", 1.0),
            "home_strength": features.get("home_avg_goals", self._league_avg_goals),
            "away_strength": features.get("away_avg_goals", self._league_avg_goals),
        }
