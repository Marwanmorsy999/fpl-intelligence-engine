import os

files = {}

# 1. Assist Model
files['src/fpl_intelligence/prediction/advanced_player/assist_model.py'] = '''"""Assist prediction model for Phase 5.

Estimates P(0 assists), P(1 assist), P(2+ assists) for a player-fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class AssistPrediction:
    """Predicted assist distribution."""

    player_id: int
    fixture_id: int
    expected_assists: float = 0.0
    p_0: float = 1.0
    p_1: float = 0.0
    p_2_plus: float = 0.0
    distribution: dict[int, float] = field(default_factory=dict)
    data_completeness: float = 0.0
    method: str = "assist_model_v1"
    xa_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "fixture_id": self.fixture_id,
            "expected_assists": round(self.expected_assists, 4),
            "p_0": round(self.p_0, 4),
            "p_1": round(self.p_1, 4),
            "p_2_plus": round(self.p_2_plus, 4),
            "distribution": {str(k): round(v, 4) for k, v in self.distribution.items()},
            "data_completeness": round(self.data_completeness, 4),
            "method": self.method,
            "xa_used": self.xa_used,
        }


class AssistModel:
    """Player assist distribution model."""

    def __init__(self, default_lambda: float = 0.15, max_assists: int = 3) -> None:
        self._default_lambda = default_lambda
        self._max_assists = max_assists

    @property
    def model_name(self) -> str:
        return "assist_model_v1"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        context: dict[str, Any] | None = None,
    ) -> AssistPrediction:
        context = context or {}
        xa = features.get("xa_last_5", features.get("xa_last_3", 0.0))
        xa_used = "xa_last_5" in features or "xa_last_3" in features

        assists_per_90 = features.get("assists_per_90", features.get("assists_last_5", 0.0) / 5.0)
        assists_per_90 = max(0.0, assists_per_90)

        team_xg = features.get("team_expected_goals", 1.4)
        key_passes = features.get("key_passes_last_5", 0.0)
        is_home = features.get("is_home", 0.5)
        expected_minutes = features.get("expected_minutes", 60.0)

        fixture_factor = team_xg / 1.4
        if is_home == 1.0:
            fixture_factor *= 1.05
        elif is_home == 0.0:
            fixture_factor *= 0.95

        minutes_factor = min(1.0, max(0.0, expected_minutes / 90.0))
        key_pass_factor = 1.0 + min(0.5, key_passes / 20.0)

        if xa_used and xa > 0:
            xa_per_90 = xa / max(1.0, expected_minutes / 90.0)
            lambda_val = (assists_per_90 * 0.4 + xa_per_90 * 0.6) * fixture_factor * minutes_factor * key_pass_factor
        else:
            lambda_val = assists_per_90 * fixture_factor * minutes_factor * key_pass_factor

        lambda_val = max(0.01, min(3.0, lambda_val))

        position_code = features.get("position_code", 3)
        if position_code == 1:
            lambda_val *= 0.02
        elif position_code == 2:
            lambda_val *= 0.7
        elif position_code == 4:
            lambda_val *= 1.1

        probs = self._poisson_truncated(lambda_val, self._max_assists)

        p_0 = probs.get(0, 1.0)
        p_1 = probs.get(1, 0.0)
        p_2_plus = sum(probs.get(k, 0.0) for k in range(2, self._max_assists + 1))

        expected = sum(k * probs.get(k, 0.0) for k in range(self._max_assists + 1))
        completeness = self._compute_completeness(features, xa_used)

        return AssistPrediction(
            player_id=player_id,
            fixture_id=fixture_id,
            expected_assists=round(expected, 4),
            p_0=round(p_0, 4),
            p_1=round(p_1, 4),
            p_2_plus=round(p_2_plus, 4),
            distribution={k: round(v, 4) for k, v in probs.items()},
            data_completeness=completeness,
            method=self.model_name,
            xa_used=xa_used,
        )

    def _poisson_truncated(self, lam: float, max_k: int) -> dict[int, float]:
        probs: dict[int, float] = {}
        for k in range(max_k + 1):
            if lam <= 0:
                probs[k] = 1.0 if k == 0 else 0.0
            else:
                probs[k] = float(np.exp(-lam) * (lam**k) / max(1, np.math.factorial(k)))
        total = sum(probs.values())
        if total > 0:
            probs = {k: v / total for k, v in probs.items()}
        return probs

    def _compute_completeness(self, features: dict[str, float], xa_used: bool) -> float:
        score = 0.0
        total = 0.0
        for key in ["assists_per_90", "assists_last_5", "expected_minutes", "team_expected_goals"]:
            total += 1.0
            if key in features and features[key] is not None:
                score += 1.0
        total += 1.0
        if xa_used:
            score += 1.0
        return round(score / total, 4) if total > 0 else 0.0
'''

# 2. Clean Sheet Model
files['src/fpl_intelligence/prediction/advanced_player/clean_sheet_model.py'] = '''"""Clean-sheet prediction model for Phase 5.

Separates team clean-sheet probability from player appearance probability.

P(player gets clean-sheet points) = P(team clean sheet) * P(player plays sufficient minutes)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CleanSheetPrediction:
    """Predicted clean-sheet probability for a player."""

    player_id: int
    fixture_id: int
    team_clean_sheet_probability: float = 0.0
    player_appearance_probability: float = 0.0
    joint_probability: float = 0.0
    data_completeness: float = 0.0
    method: str = "clean_sheet_model_v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "fixture_id": self.fixture_id,
            "team_clean_sheet_probability": round(self.team_clean_sheet_probability, 4),
            "player_appearance_probability": round(self.player_appearance_probability, 4),
            "joint_probability": round(self.joint_probability, 4),
            "data_completeness": round(self.data_completeness, 4),
            "method": self.method,
        }


class CleanSheetModel:
    """Player-level clean-sheet probability model.

    Keeps team probability and player appearance probability separate.
    """

    def __init__(self) -> None:
        pass

    @property
    def model_name(self) -> str:
        return "clean_sheet_model_v1"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        context: dict[str, Any] | None = None,
    ) -> CleanSheetPrediction:
        context = context or {}

        team_cs_prob = features.get("team_clean_sheet_probability", 0.0)
        expected_minutes = features.get("expected_minutes", 60.0)
        probability_starting = features.get("probability_starting", 0.5)

        # Player appearance probability: P(minutes >= 60 for CS eligibility).
        # For GK/DEF, clean sheet requires full 60+ minutes; for MID, any minutes count.
        position_code = features.get("position_code", 3)
        if position_code in (1, 2):  # GK, DEF
            appearance_prob = probability_starting * min(1.0, expected_minutes / 60.0)
        else:
            appearance_prob = min(1.0, probability_starting * (expected_minutes / 60.0))

        joint = team_cs_prob * appearance_prob
        completeness = self._compute_completeness(features)

        return CleanSheetPrediction(
            player_id=player_id,
            fixture_id=fixture_id,
            team_clean_sheet_probability=round(team_cs_prob, 4),
            player_appearance_probability=round(appearance_prob, 4),
            joint_probability=round(joint, 4),
            data_completeness=completeness,
            method=self.model_name,
        )

    def _compute_completeness(self, features: dict[str, float]) -> float:
        needed = ["team_clean_sheet_probability", "expected_minutes", "probability_starting"]
        present = sum(1 for k in needed if k in features)
        return round(present / len(needed), 4)
'''

# Write files
for path, content in files.items():
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)
    print(f'Created: {path}')
