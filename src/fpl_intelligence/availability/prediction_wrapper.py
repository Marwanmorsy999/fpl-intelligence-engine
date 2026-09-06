"""Phase 7 prediction-provider wrapper.

Wraps a :class:`DecisionPredictionProvider` and adjusts predictions using
availability state. This is the entry point for the decision engine to
incorporate availability intelligence.

Pipeline:
    Base Prediction → Availability State → Adjusted Prediction → Distribution → Decision
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import numpy as np

from fpl_intelligence.availability.providers import AvailabilityProvider
from fpl_intelligence.availability.state import state_to_adjustment
from fpl_intelligence.optimization.provider import (
    DecisionPredictionProvider,
    PlayerPrediction,
)


class AvailabilityAwarePredictionProvider(DecisionPredictionProvider):
    """Wraps a base provider and adjusts predictions with availability state.

    Uses confidence-weighted blending (same principle as
    :class:`AvailabilityAwareMinutesModel`) to avoid overwriting model
    probabilities with arbitrary fixed numbers. When no availability evidence
    exists, predictions pass through unchanged.
    """

    def __init__(self, base: DecisionPredictionProvider, availability: AvailabilityProvider):
        self._base = base
        self._availability = availability

    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        base = self._base.get_player_prediction(player_id, gameweek)
        adj = self._adjust(base, player_id, gameweek)
        return PlayerPrediction(
            player_id=player_id,
            gameweek=gameweek,
            expected_points=adj["expected_points"],
            expected_minutes=adj["expected_minutes"],
            start_probability=adj["start_probability"],
            distribution=adj["distribution"],
            floor=adj["floor"],
            ceiling=adj["ceiling"],
        )

    def get_squad_predictions(self, squad_players: list[int], gws: list[int]) -> dict:
        result: dict[int, dict[int, PlayerPrediction]] = {}
        for gw in gws:
            result[gw] = {pid: self.get_player_prediction(pid, gw) for pid in squad_players}
        return result

    def get_all_predictions(self, gameweek: int) -> dict:
        base_preds = self._base.get_all_predictions(gameweek)
        return {
            pid: PlayerPrediction(
                player_id=pid,
                gameweek=gameweek,
                expected_points=adj["expected_points"],
                expected_minutes=adj["expected_minutes"],
                start_probability=adj["start_probability"],
                distribution=adj["distribution"],
                floor=adj["floor"],
                ceiling=adj["ceiling"],
                confidence=pred.confidence,
                data_completeness=pred.data_completeness,
            )
            for pid, pred in base_preds.items()
            for adj in [self._adjust(pred, pid, gameweek)]
        }

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        return self._base.get_fixture_count(player_id, gameweek)

    # ------------------------------------------------------------------
    # Internal adjustment logic
    # ------------------------------------------------------------------

    def _adjust(self, base: PlayerPrediction, player_id: int, gameweek: int) -> dict[str, Any]:
        """Confidence-weighted blend of base prediction and availability."""
        from datetime import datetime

        game_time = datetime.now(UTC)
        status, confidence, _sources = self._availability.get_availability(player_id, game_time)

        if confidence < 0.01:
            # No evidence — pass through.
            return {
                "expected_points": base.expected_points,
                "expected_minutes": base.expected_minutes,
                "start_probability": base.start_probability,
                "distribution": base.distribution,
                "floor": base.floor,
                "ceiling": base.ceiling,
            }

        adjustment = state_to_adjustment(status, confidence)
        c = min(1.0, max(0.0, confidence))

        # Adjust start probability.
        base_start = base.start_probability or 1.0
        avail_start = adjustment["start_probability"]
        adj_start = base_start * (1.0 - c) + avail_start * c

        # Adjust expected minutes proportionally.
        base_minutes = base.expected_minutes or 0.0
        avail_minutes_factor = adjustment["minutes_factor"]
        adj_minutes = base_minutes * (1.0 - c) + (base_minutes * avail_minutes_factor) * c

        # Adjust expected points proportionally to start probability change.
        base_points = base.expected_points or 0.0
        # Points scale with start probability (if a player doesn't start,
        # they score ~0 points). This is a proportional adjustment, not a
        # direct overwrite.
        points_ratio = adj_start / base_start if base_start > 0 else 0.0 if adj_start == 0 else 1.0
        adj_points = base_points * (1.0 - c) + (base_points * points_ratio) * c

        # Adjust distribution.
        dist = base.distribution
        if dist is not None and len(dist) > 0:
            # Scale the distribution mean by the points ratio.
            adj_dist = dist * (1.0 - c) + (dist * points_ratio) * c
        else:
            adj_dist = np.array([adj_points])

        # Adjust floor/ceiling.
        floor = base.floor or 0.0
        ceiling = base.ceiling or (base_points * 2.0)
        adj_floor = floor * (1.0 - c) + 0.0 * c  # OUT/suspended → 0 floor
        adj_ceiling = ceiling * (1.0 - c) + (ceiling * avail_start) * c

        return {
            "expected_points": round(adj_points, 4),
            "expected_minutes": round(adj_minutes, 4),
            "start_probability": round(adj_start, 4),
            "distribution": adj_dist,
            "floor": round(adj_floor, 4),
            "ceiling": round(adj_ceiling, 4),
        }
