"""Regression tests for request-local prediction reuse in decision optimization."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
from fpl_intelligence.squad.models import SquadStateCreate


def _prediction(player_id: int, gameweek: int, expected_points: float = 5.0) -> PlayerPrediction:
    distribution = np.array([expected_points - 1.0, expected_points, expected_points + 1.0])
    return PlayerPrediction(
        player_id=player_id,
        gameweek=gameweek,
        expected_points=expected_points,
        expected_minutes=90.0,
        start_probability=1.0,
        distribution=distribution,
        floor=float(np.min(distribution)),
        ceiling=float(np.max(distribution)),
    )


def _positions() -> dict[int, int]:
    return {
        **{1: 1, 2: 1},
        **{i: 2 for i in range(3, 8)},
        **{i: 3 for i in range(8, 13)},
        **{i: 4 for i in range(13, 16)},
    }


def test_timed_provider_reuses_same_player_gameweek_prediction() -> None:
    provider = MagicMock()
    prediction = _prediction(1, 5)
    provider.get_player_prediction.return_value = prediction

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    first = timed.get_player_prediction(1, 5)
    second = timed.get_player_prediction(1, 5)

    assert first is second
    provider.get_player_prediction.assert_called_once_with(1, 5)


def test_timed_provider_keeps_gameweeks_independent() -> None:
    provider = MagicMock()
    provider.get_player_prediction.side_effect = lambda pid, gw: _prediction(pid, gw)

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    gw5 = timed.get_player_prediction(1, 5)
    gw6 = timed.get_player_prediction(1, 6)
    gw5_again = timed.get_player_prediction(1, 5)

    assert gw5 is gw5_again
    assert gw5 is not gw6
    assert provider.get_player_prediction.call_count == 2


def test_bulk_predictions_populate_shared_cache() -> None:
    provider = MagicMock()
    prediction = _prediction(1, 5)
    provider.get_squad_predictions.return_value = {5: {1: prediction}}

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    result = timed.get_squad_predictions([1], [5])
    cached = timed.get_player_prediction(1, 5)

    assert cached is result[5][1]
    provider.get_squad_predictions.assert_called_once_with([1], [5])
    provider.get_player_prediction.assert_not_called()


def test_bulk_predictions_do_not_crosswire_player_and_gameweek_keys() -> None:
    provider = MagicMock()
    prediction = _prediction(5, 1)
    provider.get_squad_predictions.return_value = {1: {5: prediction}}
    provider.get_player_prediction.return_value = _prediction(1, 5, 99.0)

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    timed.get_squad_predictions([5], [1])
    direct = timed.get_player_prediction(1, 5)

    assert direct.player_id == 1
    assert direct.gameweek == 5
    provider.get_player_prediction.assert_called_once_with(1, 5)


def test_all_predictions_are_reused_per_gameweek() -> None:
    provider = MagicMock()
    prediction = _prediction(1, 5)
    provider.get_all_predictions.return_value = {1: prediction}

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    first = timed.get_all_predictions(5)
    second = timed.get_all_predictions(5)

    assert first is second
    assert first[1] is prediction
    provider.get_all_predictions.assert_called_once_with(5)


def test_all_prediction_cache_is_gameweek_scoped() -> None:
    provider = MagicMock()
    gw5 = {1: _prediction(1, 5)}
    gw6 = {1: _prediction(1, 6)}
    provider.get_all_predictions.side_effect = [gw5, gw6]

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    first = timed.get_all_predictions(5)
    second = timed.get_all_predictions(6)
    first_again = timed.get_all_predictions(5)

    assert first is first_again
    assert first is not second
    assert provider.get_all_predictions.call_count == 2


def test_request_cache_is_cleared_between_generate_decisions() -> None:
    provider = MagicMock()
    provider.get_player_prediction.side_effect = lambda pid, gw: _prediction(pid, gw)
    provider.get_fixture_count.return_value = 1
    provider.get_all_predictions.return_value = {
        pid: _prediction(pid, 1) for pid in range(1, 16)
    }

    bridge = DecisionOptimizerBridge(provider=provider)
    squad = SquadStateCreate(
        player_ids=list(range(1, 16)),
        captain_id=1,
        vice_captain_id=2,
        gameweek=1,
        player_positions=_positions(),
    )

    bridge.generate_decisions(squad)
    first_request_player_calls = provider.get_player_prediction.call_count
    first_request_pool_calls = provider.get_all_predictions.call_count

    bridge.generate_decisions(squad)
    second_request_player_calls = provider.get_player_prediction.call_count - first_request_player_calls
    second_request_pool_calls = provider.get_all_predictions.call_count - first_request_pool_calls

    assert first_request_player_calls > 0
    assert first_request_pool_calls > 0
    assert second_request_player_calls == first_request_player_calls
    assert second_request_pool_calls == first_request_pool_calls


def test_decision_optimizers_share_unique_player_gameweek_predictions() -> None:
    provider = MagicMock()
    provider.get_player_prediction.side_effect = lambda pid, gw: _prediction(
        pid, gw, float(pid)
    )
    provider.get_fixture_count.return_value = 1
    provider.get_all_predictions.return_value = {
        pid: _prediction(pid, 1, float(pid)) for pid in range(1, 16)
    }

    bridge = DecisionOptimizerBridge(provider=provider)
    squad = SquadStateCreate(
        player_ids=list(range(1, 16)),
        captain_id=1,
        vice_captain_id=2,
        gameweek=1,
        player_positions=_positions(),
    )

    bridge.generate_decisions(squad)

    # Starting XI + captain + transfer/chip paths may request the same
    # player/GW, but the underlying provider must resolve each unique key only
    # once per request.
    calls = provider.get_player_prediction.call_args_list
    keys = [(call.args[0], call.args[1]) for call in calls]
    assert len(keys) == len(set(keys))
