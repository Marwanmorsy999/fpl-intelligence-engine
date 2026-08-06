"""Goal prediction model for Phase 5 advanced player model.

Estimates a full goal-count distribution for a player in an upcoming fixture:

    P(0 goals), P(1 goal), P(2 goals), P(3+ goals)

Inputs include player xG, shots, shots in box, big chances, touches in box,
team expected goals, opponent defensive strength, home/away, expected minutes,
and recent role. Historical cutoff integrity is mandatory.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class GoalPrediction:
    """Predicted goal distribution for a player-fixture pair."""

    player_id: int
    fixture_id: int
    expected_goals: float = 0.0
    p_0: float = 1.0
    p_1: float = 0.0
    p_2: float = 0.0
    p_3_plus: float = 0.0
    distribution: dict[int, float] = field(default_factory=dict)
    data_completeness: float = 0.0
    method: str = "goal_model_v1"
    xg_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "fixture_id": self.fixture_id,
            "expected_goals": round(self.expected_goals, 4),
            "p_0": round(self.p_0, 4),
            "p_1": round(self.p_1, 4),
            "p_2": round(self.p_2, 4),
            "p_3_plus": round(self.p_3_plus, 4),
            "distribution": {str(k): round(v, 4) for k, v in self.distribution.items()},
            "data_completeness": round(self.data_completeness, 4),
            "method": self.method,
            "xg_used": self.xg_used,
        }


class GoalModel:
    """Player goal distribution model.

    This is a structured baseline implementation. The model uses:

    1. Player historical goal rates (per 90) from available data.
    2. Fixture context (team xG, opponent defensive strength, home/away).
    3. Expected minutes from the MinutesModel.

    Because FPL goal points vary by position, the scoring engine handles
    the conversion. This model only provides the goal-count distribution.
    """

    def __init__(
        self,
        minutes_model: Any = None,
        default_lambda: float = 0.25,
        max_goals: int = 5,
    ) -> None:
        self._minutes_model = minutes_model
        self._default_lambda = default_lambda
        self._max_goals = max_goals

    @property
    def model_name(self) -> str:
        return "goal_model_v1"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        context: dict[str, Any] | None = None,
    ) -> GoalPrediction:
        """Predict goal distribution for a player-fixture pair."""
        context = context or {}
        xg = features.get("xg_last_5", features.get("xg_last_3", 0.0))
        xg_used = "xg_last_5" in features or "xg_last_3" in features

        goals_per_90 = features.get("goals_per_90", features.get("goals_last_5", 0.0) / 5.0)
        goals_per_90 = max(0.0, goals_per_90)

        team_xg = features.get("team_expected_goals", 1.4)
        opponent_defence = features.get("opponent_defensive_strength", 1.0)
        is_home = features.get("is_home", 0.5)

        fixture_factor = 1.0
        if opponent_defence > 0:
            fixture_factor = min(2.0, max(0.3, team_xg / 1.4 * (1.0 / opponent_defence)))
        if is_home == 1.0:
            fixture_factor *= 1.05
        elif is_home == 0.0:
            fixture_factor *= 0.95

        expected_minutes = features.get("expected_minutes", 60.0)
        minutes_factor = min(1.0, max(0.0, expected_minutes / 90.0))

        if xg_used and xg > 0:
            xg_per_90 = xg / max(1.0, expected_minutes / 90.0)
            lambda_val = (goals_per_90 * 0.4 + xg_per_90 * 0.6) * fixture_factor * minutes_factor
        else:
            lambda_val = goals_per_90 * fixture_factor * minutes_factor

        lambda_val = max(0.01, min(5.0, lambda_val))

        position_code = features.get("position_code", 3)
        if position_code == 1:
            lambda_val *= 0.05
        elif position_code == 2:
            lambda_val *= 0.5
        elif position_code == 4:
            lambda_val *= 1.2

        probs = self._poisson_with_overdispersion(lambda_val, self._max_goals)

        p_0 = probs.get(0, 1.0)
        p_1 = probs.get(1, 0.0)
        p_2 = probs.get(2, 0.0)
        p_3_plus = sum(probs.get(k, 0.0) for k in range(3, self._max_goals + 1))

        expected = sum(k * probs.get(k, 0.0) for k in range(self._max_goals + 1))
        completeness = self._compute_completeness(features, xg_used)

        return GoalPrediction(
            player_id=player_id,
            fixture_id=fixture_id,
            expected_goals=round(expected, 4),
            p_0=round(p_0, 4),
            p_1=round(p_1, 4),
            p_2=round(p_2, 4),
            p_3_plus=round(p_3_plus, 4),
            distribution={k: round(v, 4) for k, v in probs.items()},
            data_completeness=completeness,
            method=self.model_name,
            xg_used=xg_used,
        )

    def _poisson_with_overdispersion(
        self, lam: float, max_k: int
    ) -> dict[int, float]:
        """Compute a Poisson-like distribution."""
        probs: dict[int, float] = {}
        for k in range(max_k + 1):
            if lam <= 0:
                probs[k] = 1.0 if k == 0 else 0.0
            else:
                probs[k] = float(np.exp(-lam) * (lam**k) / max(1, math.factorial(k)))
        total = sum(probs.values())
        if total > 0:
            probs = {k: v / total for k, v in probs.items()}
        return probs

    def _compute_completeness(self, features: dict[str, float], xg_used: bool) -> float:
        score = 0.0
        total = 0.0
        for key in ["goals_per_90", "goals_last_5", "expected_minutes", "team_expected_goals"]:
            total += 1.0
            if key in features and features[key] is not None:
                score += 1.0
        total += 1.0
        if xg_used:
            score += 1.0
        return round(score / total, 4) if total > 0 else 0.0
