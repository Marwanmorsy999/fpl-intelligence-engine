"""Team-level feature calculator.

Calculates team-level features using only data available before the cutoff.

Features:
    - avg_goals_scored
    - avg_goals_conceded
    - avg_xg
    - avg_xg_conceded
    - avg_shots
    - avg_shots_on_target
    - avg_possession
    - clean_sheet_rate
    - home_advantage
    - recent_form (last 5 matches)
    - defensive_strength
    - attack_strength
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import TeamMatchPerformance
from fpl_intelligence.features.calculators.base import BaseFeatureCalculator
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
    apply_policy,
)


class TeamFeaturesCalculator(BaseFeatureCalculator):
    """Calculates team-level features using historical match data."""

    @property
    def feature_name(self) -> str:
        return "team_features"

    @property
    def version(self) -> str:
        return "1.0.0"

    def compute(
        self,
        entity_id: int,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compute team features at a cutoff.

        Args:
            entity_id: Team ID.
            cutoff_time: The historical decision cutoff.
            context: Must contain 'db' session and optionally 'policy'.

        Returns:
            Dict with team features and metadata.
        """
        ctx = context or {}
        db: Session = ctx.get("db")  # type: ignore[assignment]
        if db is None:
            raise ValueError("context must contain 'db' session")

        policy: InformationAccessPolicy = ctx.get("policy", DEFAULT_POLICY)

        # Query team match performances before cutoff
        stmt = select(TeamMatchPerformance).where(
            TeamMatchPerformance.team_id == entity_id,
        )
        try:
            condition = apply_policy(TeamMatchPerformance, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass

        perfs = list(db.execute(stmt).scalars().all())

        if not perfs:
            return self._make_result(
                value={},
                is_missing=True,
                completeness_score=0.0,
                source_count=0,
            )

        value: dict[str, Any] = {}

        n = len(perfs)
        value["match_count"] = n

        # Average goals
        value["avg_goals_scored"] = sum(p.goals_scored or 0 for p in perfs) / n
        value["avg_goals_conceded"] = sum(p.goals_conceded or 0 for p in perfs) / n

        # Expected goals
        value["avg_xg"] = sum(p.expected_goals or 0.0 for p in perfs) / n
        value["avg_xg_conceded"] = sum(p.expected_goals_conceded or 0.0 for p in perfs) / n

        # Shots
        value["avg_shots"] = sum(p.shots or 0 for p in perfs) / n
        value["avg_shots_on_target"] = sum(p.shots_on_target or 0 for p in perfs) / n

        # Possession
        value["avg_possession"] = sum(p.possession or 0.0 for p in perfs) / n

        # Clean sheet rate
        clean_sheets = sum(1 for p in perfs if (p.goals_conceded or 0) == 0)
        value["clean_sheet_rate"] = clean_sheets / n

        # Home advantage
        home_perfs = [p for p in perfs if p.is_home]
        away_perfs = [p for p in perfs if not p.is_home]
        if home_perfs and away_perfs:
            home_goals = sum(p.goals_scored or 0 for p in home_perfs) / len(home_perfs)
            away_goals = sum(p.goals_scored or 0 for p in away_perfs) / len(away_perfs)
            value["home_advantage"] = home_goals - away_goals
        else:
            value["home_advantage"] = 0.0

        # Recent form (last 5 matches)
        recent = perfs[-5:]
        if recent:
            value["recent_goals_scored"] = sum(p.goals_scored or 0 for p in recent)
            value["recent_goals_conceded"] = sum(p.goals_conceded or 0 for p in recent)
            value["recent_points"] = sum(p.goals_scored or 0 for p in recent) * 4  # Simplified

        # Strength metrics
        league_avg_goals = 1.4  # Approximate Premier League average
        value["attack_strength"] = value["avg_goals_scored"] / league_avg_goals if league_avg_goals > 0 else 1.0
        value["defensive_strength"] = league_avg_goals / value["avg_goals_conceded"] if value["avg_goals_conceded"] > 0 else 1.0

        # Latest match info
        latest = perfs[-1]
        value["latest_match_time"] = latest.fixture_id

        return self._make_result(
            value=value,
            is_missing=False,
            completeness_score=min(1.0, n / 10.0),
            source_count=n,
            latest_source_time=latest.available_at,
        )

    def _get_all_entity_ids(
        self,
        db: Any,
        cutoff_time: datetime,
        context: dict[str, Any],
    ) -> list[int]:
        """Get all team IDs that have match data before cutoff."""
        policy: InformationAccessPolicy = context.get("policy", DEFAULT_POLICY)
        stmt = select(TeamMatchPerformance.team_id).distinct()
        try:
            condition = apply_policy(TeamMatchPerformance, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass
        return list(db.execute(stmt).scalars().all())
