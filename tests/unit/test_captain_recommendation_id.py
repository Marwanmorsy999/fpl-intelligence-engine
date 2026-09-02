"""Regression tests for captain recommendation identity propagation."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from fpl_intelligence.optimization.domain import ActionType, SquadState
from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.optimization.rules import FPLRules
from fpl_intelligence.optimization.squad import CaptainOptimizer, StartingXIOptimizer
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge


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
    predictions = {
        101: _prediction(101, 2.0),
        202: _prediction(202, 8.0),
    }
    provider.get_squad_predictions.return_value = {3: predictions}

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
    provider.get_squad_predictions.assert_called_once_with([101, 202], [3])
    provider.get_player_prediction.assert_not_called()


def test_captain_batch_fetch_falls_back_for_missing_player() -> None:
    provider = MagicMock()
    provider.get_squad_predictions.return_value = {3: {101: _prediction(101, 2.0)}}
    provider.get_player_prediction.return_value = _prediction(202, 8.0)

    optimizer = CaptainOptimizer(provider)
    candidates = [101, 202]
    evaluated = optimizer.evaluate_candidates(candidates, 3)

    assert set(evaluated) == {101, 202}
    provider.get_squad_predictions.assert_called_once_with(candidates, [3])
    provider.get_player_prediction.assert_called_once_with(202, 3)


def test_starting_xi_uses_one_batched_prediction_fetch() -> None:
    provider = MagicMock()
    players = list(range(1, 16))
    provider.get_squad_predictions.return_value = {
        3: {pid: _prediction(pid, float(pid)) for pid in players}
    }

    optimizer = StartingXIOptimizer(provider, FPLRules())
    positions = {
        1: 1,
        2: 2,
        3: 2,
        4: 2,
        5: 2,
        6: 2,
        7: 3,
        8: 3,
        9: 3,
        10: 3,
        11: 3,
        12: 4,
        13: 4,
        14: 4,
        15: 4,
    }

    starting_xi, bench = optimizer.optimize_xi(players, 3, positions)

    assert len(starting_xi) == 11
    assert sorted(starting_xi + bench) == players
    provider.get_squad_predictions.assert_called_once_with(players, [3])
    provider.get_player_prediction.assert_not_called()


def test_timed_batch_provider_skips_all_cached_pairs() -> None:
    provider = MagicMock()
    timed = DecisionOptimizerBridge(provider=provider)._timed_provider
    first = _prediction(123, 6.0)
    second = _prediction(456, 7.0)
    timed._prediction_cache[(123, 3)] = first
    timed._prediction_cache[(456, 3)] = second

    result = timed.get_squad_predictions([123, 456], [3])

    assert result == {3: {123: first, 456: second}}
    provider.get_squad_predictions.assert_not_called()


def test_timed_batch_provider_fetches_only_missing_pairs() -> None:
    provider = MagicMock()
    timed = DecisionOptimizerBridge(provider=provider)._timed_provider
    cached = _prediction(123, 6.0)
    missing = _prediction(456, 7.0)
    timed._prediction_cache[(123, 3)] = cached
    provider.get_squad_predictions.return_value = {3: {456: missing}}

    result = timed.get_squad_predictions([123, 456], [3])

    assert result == {3: {123: cached, 456: missing}}
    provider.get_squad_predictions.assert_called_once_with([456], [3])
