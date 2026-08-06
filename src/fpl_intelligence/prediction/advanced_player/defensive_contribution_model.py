"""Defensive contribution prediction model for Phase 5.

Builds only where historical data supports a reliable target.
Separates:
- probability of reaching threshold
- expected defensive contribution points
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DefensiveContributionPrediction:
    """Predicted defensive contribution for a player."""

    player_id: int
    fixture_id: int
    expected_points: float = 0.0
    probability_threshold_met: float = 0.0
    data_completeness: float = 0.0
    available: bool = True
    coverage: float = 0.0
    method: str = "defensive_contribution_model_v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "fixture_id": self.fixture_id,
            "expected_points": round(self.expected_points, 4),
            "probability_threshold_met": round(self.probability_threshold_met, 4),
            "data_completeness": round(self.data_completeness, 4),
            "available": self.available,
            "coverage": round(self.coverage, 4),
            "method": self.method,
        }


class DefensiveContributionModel:
    """Player defensive contribution model.

    FPL awards points for defensive actions (tackles, clearances, blocks,
    interceptions, recoveries) when a player meets a threshold. This model
    estimates the probability of meeting that threshold and the expected
    points.

    Requires sufficient historical data coverage. If coverage is below
    threshold, marks as unavailable.
    """

    MIN_COVERAGE = 0.6  # Require 60% defensive action data availability.

    def __init__(self) -> None:
        self._coverage: float = 1.0

    @property
    def model_name(self) -> str:
        return "defensive_contribution_model_v1"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def set_coverage(self, coverage: float) -> None:
        """Set observed defensive data coverage (0-1)."""
        self._coverage = max(0.0, min(1.0, coverage))

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        context: dict[str, Any] | None = None,
    ) -> DefensiveContributionPrediction:
        """Predict defensive contribution for a player-fixture pair."""
        if self._coverage < self.MIN_COVERAGE:
            return DefensiveContributionPrediction(
                player_id=player_id,
                fixture_id=fixture_id,
                available=False,
                coverage=self._coverage,
                data_completeness=0.0,
                method=self.model_name,
            )

        context = context or {}
        expected_minutes = features.get("expected_minutes", 60.0)

        # Defensive actions per 90.
        tackles = features.get("tackles_last_5", 0.0) / 5.0
        clearances = features.get("clearances_last_5", 0.0) / 5.0
        blocks = features.get("blocks_last_5", 0.0) / 5.0
        interceptions = features.get("interceptions_last_5", 0.0) / 5.0
        recoveries = features.get("recoveries_last_5", 0.0) / 5.0

        # Combined defensive action rate.
        total_actions_per_90 = tackles + clearances + blocks + interceptions + recoveries

        # FPL threshold: typically 2+ defensive actions in a match.
        # Probability scales with action rate and minutes.
        minutes_factor = min(1.0, max(0.0, expected_minutes / 90.0))
        expected_actions = total_actions_per_90 * minutes_factor

        # Probability of meeting threshold (2+ actions).
        if expected_actions >= 4.0:
            prob_threshold = 0.8
        elif expected_actions >= 3.0:
            prob_threshold = 0.5
        elif expected_actions >= 2.0:
            prob_threshold = 0.25
        else:
            prob_threshold = 0.05

        # Expected defensive contribution points (typically 1 point when threshold met).
        expected_pts = prob_threshold * 1.0

        completeness = self._compute_completeness(features)

        return DefensiveContributionPrediction(
            player_id=player_id,
            fixture_id=fixture_id,
            expected_points=round(expected_pts, 4),
            probability_threshold_met=round(prob_threshold, 4),
            data_completeness=completeness,
            available=True,
            coverage=self._coverage,
            method=self.model_name,
        )

    def _compute_completeness(self, features: dict[str, float]) -> float:
        needed = [
            "tackles_last_5",
            "clearances_last_5",
            "blocks_last_5",
            "interceptions_last_5",
            "recoveries_last_5",
            "expected_minutes",
        ]
        present = sum(1 for k in needed if k in features)
        return round(present / len(needed), 4)
