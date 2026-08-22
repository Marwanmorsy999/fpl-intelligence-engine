from fpl_intelligence.optimization.backtesting import simulate_decision
from fpl_intelligence.optimization.domain import (
    ActionType,
    CandidateAction,
    SquadState,
)
from fpl_intelligence.optimization.rules import RULES_2026_27, FPLRules


def test_2026_27_chip_rules():
    rules = FPLRules(RULES_2026_27)
    assert rules.is_half_season_chips is True
    assert rules.get_half_season(1) == 1
    assert rules.get_half_season(19) == 1
    assert rules.get_half_season(20) == 2
    assert rules.get_half_season(38) == 2
    assert rules.get_chip_count("wildcard") == 2


def test_mock_simulate_decision():
    # Basic smoke test for the new numpy-based simulate_decision
    class MockPrediction:
        def __init__(self, ev, dist=None):
            self.expected_points = ev
            self.distribution = dist if dist is not None else []

    class MockProvider:
        def get_player_prediction(self, pid, gw):
            return MockPrediction(ev=5.0)

    provider = MockProvider()
    squad = SquadState(
        manager_id=1,
        season="2025-26",
        gameweek=10,
        squad_players=list(range(1, 16)),
        starting_xi=list(range(1, 12)),
        bench_order=list(range(12, 16)),
        captain=1,
        vice_captain=2,
        bank=0.0,
        team_value=100.0,
        free_transfers=1,
        rolled_transfers=0,
        transfer_hits=0,
    )

    action = CandidateAction(action_type=ActionType.ROLL, horizon=1)

    outcome = simulate_decision(
        squad=squad, action=action, horizon=1, simulations=10, seed=42, provider=provider
    )

    # Expected: 11 players * 5.0 EV + 1 captain bonus * 5.0 EV = 60.0
    assert outcome.expected_score == 60.0
    assert outcome.median == 60.0
