"""Regression: full transfer eval is limited to top-K lightweight pairs."""

from __future__ import annotations

from unittest.mock import MagicMock

from fpl_intelligence.optimization.domain import ActionType, SquadState
from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.optimization.rules import FPLRules
from fpl_intelligence.optimization.transfers import (
    _MAX_FULL_TRANSFER_EVALS,
    MultiTransferPlanner,
    TransferEvaluation,
    TransferOptimizer,
)


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


def test_generate_candidates_limits_full_transfer_evals() -> None:
    """Many valid pairs must not all receive full distribution evaluation."""
    squad_players = list(range(1, 16))
    player_positions = {
        **{1: 1, 2: 1},
        **{i: 2 for i in range(3, 8)},
        **{i: 3 for i in range(8, 13)},
        **{i: 4 for i in range(13, 16)},
    }
    player_prices = {i: 5.0 for i in range(1, 80)}
    player_teams = {i: (i % 10) + 1 for i in range(1, 80)}

    bulk_pool = {
        i: _prediction(i, 3, 4.0 if i < 16 else 10.0 + (i % 5))
        for i in range(1, 80)
    }

    provider = MagicMock()
    provider.get_all_predictions.return_value = bulk_pool

    optimizer = TransferOptimizer(provider, FPLRules())
    eval_count = {"n": 0}

    def _fake_eval(squad, p_out, p_in, horizon=4):
        eval_count["n"] += 1
        return TransferEvaluation(
            transfers_in=[p_in],
            transfers_out=[p_out],
            hit_cost=0,
            expected_points_gain=2.0,
            net_points=2.0,
            probability_beat_roll=0.7,
            is_valid=True,
        )

    optimizer.evaluate_transfer = _fake_eval  # type: ignore[method-assign]

    planner = MultiTransferPlanner(optimizer, provider, FPLRules())
    squad = SquadState(
        manager_id=1,
        season="2026-27",
        gameweek=3,
        squad_players=squad_players,
        starting_xi=squad_players[:11],
        bench_order=squad_players[11:],
        captain=1,
        vice_captain=2,
        bank=5.0,
        team_value=100.0,
        free_transfers=1,
        rolled_transfers=0,
        transfer_hits=0,
    )

    rec = planner.generate_candidates(
        squad=squad,
        player_positions=player_positions,
        player_prices=player_prices,
        player_teams=player_teams,
        horizon=1,
    )

    assert eval_count["n"] <= _MAX_FULL_TRANSFER_EVALS
    assert eval_count["n"] > 0
    assert rec.action.action_type == ActionType.TRANSFER
    assert rec.expected_gain == 2.0
