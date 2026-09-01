"""Regression tests for captain recommendation identity propagation."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from fpl_intelligence.optimization.domain import ActionType, SquadState
from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.optimization.squad import CaptainOptimizer


def _prediction(player_id: int, expected_points: float) -> PlayerPrediction:
    distribution = np.array([expected_points, expected_points, expected_points + 1.0])
    return PlayerPrediction(
        player_id=player_id,
        gameweek=3,
        expected_points=expected_points,
        expected_minutes=90.0,
        start_probability=1.0,
        distribution=distribution,
        floor=float(np.min(distribution)),
        ceiling=float(np.max(distribution)),
    )


def test_captain_recommendation_carries_selected_player_id() -> None:
    provider = MagicMock()
    provider.get_player_prediction.side_effect = [
        _prediction(101, 2.0),
        _prediction(202, 8.0),
    ]

    optimizer = CaptainOptimizer(provider)
    squad = SquadState(
        manager_id=1,
        season="2025-26",
        gameweek=3,
        squad_players=[101, 202],
        starting_xi=[101, 202],
        bench_order=[],
        captain=101,
        vice_captain=202,
        bank=0.0,
        team_value=100.0,
        free_transfers=1,
        rolled_transfers=0,
        transfer_hits=0,
    )

    recommendation = optimizer.recommend_captain(squad)

    assert recommendation.action.action_type == ActionType.CAPTAIN
    assert recommendation.action.transfers_in == [202]
    assert recommendation.expected_gain > 0
