"""Bonus/BPS prediction model for Phase 5.

Where BPS and bonus data are sufficiently complete, models:
- probability of earning bonus
- expected bonus points
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BonusPrediction:
    """Predicted bonus points for a player."""

    player_id: int
    fixture_id: int
    expected_bonus_points: float = 0.0
    probability_bonus: float = 0.0
    data_completeness: float = 0.0
    available: bool = True
    method: str = "bonus_model_v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "fixture_id": self.fixture_id,
            "expected_bonus_points": round(self.expected_bonus_points, 4),
            "probability_bonus": round(self.probability_bonus, 4),
            "data_completeness": round(self.data_completeness, 4),
            "available": self.available,
            "method": self.method,
        }


class BonusModel:
    """Player bonus points prediction model.

    Uses BPS and match events to estimate bonus probability and expected points.
    If BPS data coverage is below threshold, marks as unavailable.
    """

    MIN_BPS_COVERAGE = 0.7

    def __init__(self) -> None:
        self._bps_coverage: float = 1.0

    @property
    def model_name(self) -> str:
        return "bonus_model_v1"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        context: dict[str, Any] | None = None,
    ) -> BonusPrediction:
        """Predict bonus points for a player-fixture pair."""
        if self._bps_coverage < self.MIN_BPS_COVERAGE:
            return BonusPrediction(
                player_id=player_id,
                fixture_id=fixture_id,
                available=False,
                data_completeness=0.0,
                method=self.model_name,
            )

        context = context or {}
        bps = features.get("bps_last_5", features.get("bps", 0.0))
        expected_minutes = features.get("expected_minutes", 60.0)
        goals = context.get("expected_goals", 0.0)
        assists = context.get("expected_assists", 0.0)

        bps_baseline = max(0.0, bps)
        goal_bps = goals * 15.0
        assist_bps = assists * 10.0
        total_bps = (bps_baseline + goal_bps + assist_bps) * min(
            1.0, max(0.3, expected_minutes / 90.0)
        )

        if total_bps >= 30:
            prob_bonus = 0.7
        elif total_bps >= 20:
            prob_bonus = 0.4
        elif total_bps >= 10:
            prob_bonus = 0.15
        else:
            prob_bonus = 0.05

        expected_pts = prob_bonus * 2.0
        completeness = self._compute_completeness(features)

        return BonusPrediction(
            player_id=player_id,
            fixture_id=fixture_id,
            expected_bonus_points=round(expected_pts, 4),
            probability_bonus=round(prob_bonus, 4),
            data_completeness=completeness,
            available=True,
            method=self.model_name,
        )

    def _compute_completeness(self, features: dict[str, float]) -> float:
        needed = ["bps_last_5", "expected_minutes"]
        present = sum(1 for k in needed if k in features)
        return round(present / len(needed), 4)
