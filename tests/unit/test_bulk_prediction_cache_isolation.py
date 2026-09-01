"""Regression tests preventing lightweight bulk predictions from poisoning full cache."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge


def _prediction(player_id: int, gameweek: int, *, full: bool) -> PlayerPrediction:
    distribution = np.array([4.0, 6.0, 8.0]) if full else np.empty(0, dtype=float)
    return PlayerPrediction(
        player_id=player_id,
        gameweek=gameweek,
        expected_points=6.0,
        expected_minutes=90.0,
        start_probability=1.0,
        distribution=distribution,
        floor=4.0,
        ceiling=8.0,
    )


def test_lightweight_bulk_prediction_does_not_fill_full_prediction_cache() -> None:
    provider = MagicMock()
    lightweight = _prediction(123, 3, full=False)
    full = _prediction(123, 3, full=True)
    provider.get_all_predictions.return_value = {123: lightweight}
    provider.get_player_prediction.return_value = full

    timed = DecisionOptimizerBridge(provider=provider)._timed_provider

    assert timed.get_all_predictions(3)[123] is lightweight
    assert timed.get_player_prediction(123, 3) is full
    provider.get_player_prediction.assert_called_once_with(123, 3)


def test_full_bulk_prediction_still_populates_shared_prediction_cache() -> None:
    provider = MagicMock()
    full = _prediction(123, 3, full=True)
    provider.get_all_predictions.return_value = {123: full}

    timed = DecisionOptimizerBridge(provider=provider)._timed_provider

    assert timed.get_all_predictions(3)[123] is full
    assert timed.get_player_prediction(123, 3) is full
    provider.get_player_prediction.assert_not_called()
