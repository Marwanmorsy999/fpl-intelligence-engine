"""Player form feature calculator.

Calculates rolling form features using only data available before the cutoff.

Features:
    - rolling_points_3gw, rolling_points_5gw, rolling_points_8gw
    - rolling_minutes_3gw, rolling_minutes_5gw
    - rolling_goals_3gw, rolling_assists_3gw
    - rolling_xg_3gw, rolling_xa_3gw
    - rolling_bps_3gw, rolling_bonus_3gw
    - form_weighted_points
    - consistency_score
    - games_started_ratio
    - recent_goals_per_90
    - recent_assists_per_90
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import PlayerGameweekPerformance
from fpl_intelligence.features.calculators.base import BaseFeatureCalculator
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
    apply_policy,
)


class PlayerFormCalculator(BaseFeatureCalculator):
    """Calculates rolling form features for a player using historical data."""

    @property
    def feature_name(self) -> str:
        return "player_form"

    @property
    def version(self) -> str:
        return "1.0.0"

    def compute(
        self,
        entity_id: int,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compute player form features at a cutoff.

        Args:
            entity_id: Player ID.
            cutoff_time: The historical decision cutoff.
            context: Must contain 'db' session and optionally 'policy'.

        Returns:
            Dict with form features and metadata.
        """
        ctx = context or {}
        db: Session = ctx.get("db")  # type: ignore[assignment]
        if db is None:
            raise ValueError("context must contain 'db' session")

        policy: InformationAccessPolicy = ctx.get("policy", DEFAULT_POLICY)

        # Query player gameweek performances before cutoff
        stmt = select(PlayerGameweekPerformance).where(
            PlayerGameweekPerformance.player_id == entity_id,
        )
        try:
            condition = apply_policy(PlayerGameweekPerformance, policy, cutoff_time)
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

        # Sort by gameweek (ascending)
        perfs_sorted = sorted(perfs, key=lambda p: p.gameweek_id or 0)

        value: dict[str, Any] = {}

        # Rolling windows
        for window in [3, 5, 8]:
            window_perfs = perfs_sorted[-window:]
            value[f"rolling_points_{window}gw"] = sum(p.total_points or 0 for p in window_perfs)
            value[f"rolling_minutes_{window}gw"] = sum(p.minutes or 0 for p in window_perfs)
            value[f"rolling_goals_{window}gw"] = sum(p.goals_scored or 0 for p in window_perfs)
            value[f"rolling_assists_{window}gw"] = sum(p.assists or 0 for p in window_perfs)
            value[f"rolling_xg_{window}gw"] = sum(p.expected_goals or 0.0 for p in window_perfs)
            value[f"rolling_xa_{window}gw"] = sum(p.expected_assists or 0.0 for p in window_perfs)
            value[f"rolling_bps_{window}gw"] = sum(p.bps or 0 for p in window_perfs)
            value[f"rolling_bonus_{window}gw"] = sum(p.bonus or 0 for p in window_perfs)

        # Weighted form (more recent = higher weight)
        if perfs_sorted:
            weights = list(range(1, len(perfs_sorted) + 1))
            total_weight = sum(weights)
            weighted_points = sum(
                (p.total_points or 0) * w for p, w in zip(perfs_sorted, weights, strict=True)
            )
            value["form_weighted_points"] = (
                weighted_points / total_weight if total_weight > 0 else 0.0
            )

        # Consistency score (coefficient of variation of points)
        points_list = [p.total_points or 0 for p in perfs_sorted]
        if len(points_list) >= 2:
            mean_pts = sum(points_list) / len(points_list)
            if mean_pts > 0:
                variance = sum((p - mean_pts) ** 2 for p in points_list) / len(points_list)
                std_pts = variance**0.5
                value["consistency_score"] = 1.0 - min(1.0, std_pts / mean_pts)
            else:
                value["consistency_score"] = 0.0
        else:
            value["consistency_score"] = 0.5

        # Games started ratio
        if perfs_sorted:
            started = sum(1 for p in perfs_sorted if (p.minutes or 0) > 0)
            value["games_started_ratio"] = started / len(perfs_sorted)

        # Per-90 rates (last 3 GWs)
        last_3 = perfs_sorted[-3:]
        total_minutes = sum(p.minutes or 0 for p in last_3)
        if total_minutes > 0:
            value["recent_goals_per_90"] = (
                sum(p.goals_scored or 0 for p in last_3) / total_minutes * 90
            )
            value["recent_assists_per_90"] = (
                sum(p.assists or 0 for p in last_3) / total_minutes * 90
            )
        else:
            value["recent_goals_per_90"] = 0.0
            value["recent_assists_per_90"] = 0.0

        # Latest snapshot info
        latest = perfs_sorted[-1]
        value["latest_gameweek"] = latest.gameweek_id
        value["latest_points"] = latest.total_points
        value["latest_price"] = latest.price

        return self._make_result(
            value=value,
            is_missing=False,
            completeness_score=min(1.0, len(perfs_sorted) / 8.0),
            source_count=len(perfs_sorted),
            latest_source_time=latest.available_at,
        )

    def _get_all_entity_ids(
        self,
        db: Any,
        cutoff_time: datetime,
        context: dict[str, Any],
    ) -> list[int]:
        """Get all player IDs that have performance data before cutoff."""
        policy: InformationAccessPolicy = context.get("policy", DEFAULT_POLICY)
        stmt = select(PlayerGameweekPerformance.player_id).distinct()
        try:
            condition = apply_policy(PlayerGameweekPerformance, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass
        return list(db.execute(stmt).scalars().all())
