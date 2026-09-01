"""Regression tests for lightweight bulk transfer-horizon prediction ranking."""

from __future__ import annotations

from unittest.mock import MagicMock

from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.optimization.rules import FPLRules
from fpl_intelligence.optimization.transfers import MultiTransferPlanner, TransferOptimizer


def _prediction(player_id: int, gameweek: int, expected_points: float) -> PlayerPrediction:
    return PlayerPrediction(
        player_id=player_id,
        gameweek=gameweek,
        expected_points=expected_points,
        expected_minutes=90.0,
        start_probability=1.0,
        distribution=[],
        floor=expected_points,
        ceiling=expected_points,
    )


def test_horizon_ranking_uses_lightweight_bulk_pools() -> None:
    provider = MagicMock()
    provider.get_all_predictions.side_effect = [
        {1: _prediction(1, 3, 5.0), 2: _prediction(2, 3, 7.0)},
        {1: _prediction(1, 4, 6.0), 2: _prediction(2, 4, 8.0)},
    ]

    planner = MultiTransferPlanner(TransferOptimizer(provider, FPLRules()), provider, FPLRules())

    totals = planner._horizon_expected_points([1, 2], start_gameweek=3, horizon=2)

    assert totals == {1: 11.0, 2: 15.0}
    assert provider.get_all_predictions.call_count == 2
    provider.get_player_prediction.assert_not_called()


def test_horizon_ranking_falls_back_only_for_missing_bulk_players() -> None:
    provider = MagicMock()
    provider.get_all_predictions.return_value = {1: _prediction(1, 3, 5.0)}
    provider.get_player_prediction.return_value = _prediction(2, 3, 9.0)

    planner = MultiTransferPlanner(TransferOptimizer(provider, FPLRules()), provider, FPLRules())

    totals = planner._horizon_expected_points([1, 2], start_gameweek=3, horizon=1)

    assert totals == {1: 5.0, 2: 9.0}
    provider.get_all_predictions.assert_called_once_with(3)
    provider.get_player_prediction.assert_called_once_with(2, 3)
