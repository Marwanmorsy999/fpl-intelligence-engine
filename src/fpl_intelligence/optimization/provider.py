"""Abstract prediction provider for the optimization engine."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class PlayerPrediction:
    """Prediction for a single player in a single gameweek."""

    player_id: int
    gameweek: int
    expected_points: float
    expected_minutes: float
    start_probability: float
    distribution: np.ndarray  # Sampled points from the predictive distribution
    floor: float  # e.g., P10
    ceiling: float  # e.g., P90
    confidence: float = 1.0  # Model confidence
    data_completeness: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "gameweek": self.gameweek,
            "expected_points": round(self.expected_points, 4),
            "expected_minutes": round(self.expected_minutes, 4),
            "start_probability": round(self.start_probability, 4),
            "floor": round(self.floor, 4),
            "ceiling": round(self.ceiling, 4),
            "confidence": round(self.confidence, 4),
            "data_completeness": round(self.data_completeness, 4),
            # Do not serialize the full distribution array by default
        }


class DecisionPredictionProvider(abc.ABC):
    """Abstract interface for predictions to be consumed by the optimizer.
    
    This ensures the optimizer doesn't hardcode dependencies on any
    specific Phase 4 or Phase 5 model implementations.
    """

    @abc.abstractmethod
    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        """Get the prediction for a specific player in a gameweek."""
        pass

    @abc.abstractmethod
    def get_squad_predictions(self, squad_players: list[int], gameweeks: list[int]) -> dict[int, dict[int, PlayerPrediction]]:
        """Get predictions for a list of players over multiple gameweeks.
        
        Returns:
            Dict mapping gameweek -> (Dict mapping player_id -> PlayerPrediction)
        """
        pass
    
    @abc.abstractmethod
    def get_all_predictions(self, gameweek: int) -> dict[int, PlayerPrediction]:
        """Get predictions for all players in a gameweek (useful for transfers)."""
        pass

    @abc.abstractmethod
    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        """Get the number of fixtures a player has in a gameweek (handles Double/Blank)."""
        pass
