import numpy as np

from fpl_intelligence.optimization.domain import (
    ActionType,
    CandidateAction,
    DecisionObjective,
    SquadState,
)
from fpl_intelligence.optimization.provider import DecisionPredictionProvider, PlayerPrediction
from fpl_intelligence.optimization.rules import FPLRules
from fpl_intelligence.optimization.squad import CaptainOptimizer, StartingXIOptimizer
from fpl_intelligence.optimization.transfers import TransferOptimizer


class MockProvider(DecisionPredictionProvider):
    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        return PlayerPrediction(
            player_id=player_id,
            gameweek=gameweek,
            expected_points=6.0,
            expected_minutes=90.0,
            start_probability=1.0,
            distribution=np.array([2, 2, 2, 6, 6, 6, 8, 10, 10, 15]),
            floor=2.0,
            ceiling=15.0,
        )

    def get_squad_predictions(self, squad_players: list[int], gameweeks: list[int]) -> dict:
        pass

    def get_all_predictions(self, gameweek: int) -> dict:
        pass

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        return 1


def test_rules():
    rules = FPLRules()
    assert rules.squad_size == 15
    assert rules.transfer_hit_cost == 4
    assert rules.min_formation(1) == 1
    assert rules.min_formation(2) == 3


def test_captain_optimizer():
    provider = MockProvider()
    optimizer = CaptainOptimizer(provider)
    squad = SquadState(
        manager_id=1,
        season="2026-27",
        gameweek=1,
        squad_players=list(range(1, 16)),
        starting_xi=list(range(1, 12)),
        bench_order=[12, 13, 14, 15],
        captain=1,
        vice_captain=2,
        bank=0.0,
        team_value=100.0,
        free_transfers=1,
        rolled_transfers=0,
        transfer_hits=0,
    )
    rec = optimizer.recommend_captain(squad)
    assert rec.action.action_type == ActionType.CAPTAIN
    # The optimizer uses the actual predictive distribution to compute EV.
    # The mock distribution [2,2,2,6,6,6,8,10,10,15] has mean 6.7, not 6.0.
    assert rec.expected_gain == 6.7


def test_transfer_optimizer():
    provider = MockProvider()
    rules = FPLRules()
    optimizer = TransferOptimizer(provider, rules)
    squad = SquadState(
        manager_id=1,
        season="2026-27",
        gameweek=1,
        squad_players=list(range(1, 16)),
        starting_xi=list(range(1, 12)),
        bench_order=[12, 13, 14, 15],
        captain=1,
        vice_captain=2,
        bank=0.0,
        team_value=100.0,
        free_transfers=0,  # 0 FT means a hit
        rolled_transfers=0,
        transfer_hits=0,
    )
    
    # 6 points out vs 6 points in = 0 EV change, hit cost is 4, net is -4
    eval_res = optimizer.evaluate_transfer(squad, player_out=1, player_in=16, horizon=1)
    assert eval_res.hit_cost == 4
    assert eval_res.net_points == -4.0
