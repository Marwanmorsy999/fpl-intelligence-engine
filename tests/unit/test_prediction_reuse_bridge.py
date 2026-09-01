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
    provider.get_squad_predictions.return_value = {1: {5: prediction}}

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    result = timed.get_squad_predictions([1], [5])
    cached = timed.get_player_prediction(1, 5)

    assert cached is result[1][5]
    provider.get_squad_predictions.assert_called_once_with([1], [5])
    provider.get_player_prediction.assert_not_called()


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
    first_request_calls = provider.get_player_prediction.call_count
    bridge.generate_decisions(squad)
    second_request_calls = provider.get_player_prediction.call_count - first_request_calls

    assert first_request_calls > 0
    assert second_request_calls == first_request_calls


def test_decision_optimizers_share_identical_prediction_objects() -> None:
    provider = MagicMock()
    provider.get_player_prediction.side_effect = lambda pid, gw: _prediction(pid, gw, float(pid))
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

    # XI + captain + transfer/chip paths may request the same player/GW, but
    # the underlying provider must resolve each unique key only once per request.
    calls = provider.get_player_prediction.call_args_list
    keys = [(call.args[0], call.args[1]) for call in calls]
    assert len(keys) == len(set(keys))
