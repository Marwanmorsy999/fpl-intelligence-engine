from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge


def _prediction(player_id: int, gameweek: int) -> PlayerPrediction:
    return PlayerPrediction(
        player_id=player_id,
        gameweek=gameweek,
        expected_points=6.0,
        expected_minutes=90.0,
        start_probability=1.0,
        distribution=np.array([4.0, 6.0, 8.0]),
        floor=4.0,
        ceiling=8.0,
    )


def test_batch_prediction_all_hits_skip_underlying_provider() -> None:
    provider = MagicMock()
    timed = DecisionOptimizerBridge(provider=provider)._timed_provider
    first = _prediction(123, 3)
    second = _prediction(456, 3)
    timed._prediction_cache[(123, 3)] = first
    timed._prediction_cache[(456, 3)] = second

    result = timed.get_squad_predictions([123, 456], [3])

    assert result == {3: {123: first, 456: second}}
    provider.get_squad_predictions.assert_not_called()


def test_batch_prediction_fetches_only_missing_pairs() -> None:
    provider = MagicMock()
    timed = DecisionOptimizerBridge(provider=provider)._timed_provider
    cached = _prediction(123, 3)
    missing = _prediction(456, 3)
    timed._prediction_cache[(123, 3)] = cached
    provider.get_squad_predictions.return_value = {3: {456: missing}}

    result = timed.get_squad_predictions([123, 456], [3])

    assert result == {3: {123: cached, 456: missing}}
    provider.get_squad_predictions.assert_called_once_with([456], [3])
