"""Regression tests for request-local fixture-count and prediction-cache reuse."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge


def test_timed_provider_reuses_fixture_count_for_same_player_gameweek() -> None:
    provider = MagicMock()
    provider.get_fixture_count.return_value = 2

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    first = timed.get_fixture_count(123, 5)
    second = timed.get_fixture_count(123, 5)

    assert first == 2
    assert second == 2
    provider.get_fixture_count.assert_called_once_with(123, 5)


def test_fixture_count_cache_is_gameweek_and_player_scoped() -> None:
    provider = MagicMock()
    provider.get_fixture_count.side_effect = [2, 0, 1]

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    player_gw5 = timed.get_fixture_count(123, 5)
    player_gw6 = timed.get_fixture_count(123, 6)
    other_player_gw5 = timed.get_fixture_count(456, 5)
    player_gw5_again = timed.get_fixture_count(123, 5)

    assert (player_gw5, player_gw6, other_player_gw5, player_gw5_again) == (2, 0, 1, 2)
    assert provider.get_fixture_count.call_count == 3


def test_request_cache_clears_fixture_counts_between_generate_decisions() -> None:
    provider = MagicMock()
    provider.get_fixture_count.return_value = 1
    bridge = DecisionOptimizerBridge(provider=provider)

    bridge._timed_provider.get_fixture_count(123, 5)
    bridge._timed_provider.get_fixture_count(123, 5)
    assert provider.get_fixture_count.call_count == 1

    bridge._timed_provider.clear_request_cache()
    bridge._timed_provider.get_fixture_count(123, 5)

    assert provider.get_fixture_count.call_count == 2


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
