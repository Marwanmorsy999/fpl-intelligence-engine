"""Clean-sheet prediction model for Phase 5.

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
