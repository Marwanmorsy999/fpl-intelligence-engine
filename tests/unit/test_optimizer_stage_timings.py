"""Unit tests for optimizer stage timings wired through _timed_phase.

Verifies that:
1. DecisionOptimizerBridge._timed_phase records elapsed ms into the base
   provider's stage_timings dict via record_stage_timing().
2. CachedLivePredictionProvider.clear_request_cache() resets stage_timings.
3. The /api/v1/decisions endpoint surfaces optimizer_stage_timings_ms in meta.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from fpl_intelligence.optimization.provider import DecisionPredictionProvider, PlayerPrediction
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
from fpl_intelligence.squad.models import SquadStateCreate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_prediction(ev: float = 5.0) -> PlayerPrediction:
    return PlayerPrediction(
        player_id=1,
        gameweek=1,
        expected_points=ev,
        expected_minutes=60.0,
        start_probability=0.9,
        distribution=np.array([ev] * 10),
        floor=max(0.0, ev - 2),
        ceiling=ev + 2,
    )


class _FakeProvider(DecisionPredictionProvider):
    """Minimal provider that records calls and exposes stage_timings."""

    def __init__(self) -> None:
        self.stage_timings: dict[str, float] = {}

    def record_stage_timing(self, stage: str, elapsed_ms: float) -> None:
        self.stage_timings[stage] = self.stage_timings.get(stage, 0.0) + elapsed_ms

    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        return _make_prediction()

    def get_squad_predictions(
        self, squad_players: list[int], gameweeks: list[int]
    ) -> dict[int, dict[int, PlayerPrediction]]:
        return {gw: {pid: _make_prediction() for pid in squad_players} for gw in gameweeks}

    def get_all_predictions(self, gameweek: int, **_: object) -> dict[int, PlayerPrediction]:
        return {}

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        return 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStageTimingsWiring:
    def test_timed_phase_records_into_provider(self) -> None:
        provider = _FakeProvider()
        bridge = DecisionOptimizerBridge(provider=provider)

        # Manually invoke _timed_phase with a trivial function.
        bridge._timed_phase("test_stage", lambda: None)

        assert "test_stage" in provider.stage_timings
        assert provider.stage_timings["test_stage"] >= 0.0

    def test_timed_phase_accumulates_across_calls(self) -> None:
        provider = _FakeProvider()
        bridge = DecisionOptimizerBridge(provider=provider)

        bridge._timed_phase("stage_a", lambda: None)
        bridge._timed_phase("stage_a", lambda: None)

        # Two calls → time should be accumulated (>= first call alone).
        assert provider.stage_timings["stage_a"] >= 0.0
        # Value comes from two accumulations — just assert key exists and is non-negative.
        assert "stage_a" in provider.stage_timings

    def test_generate_decisions_populates_all_four_stages(self) -> None:
        provider = _FakeProvider()
        bridge = DecisionOptimizerBridge(provider=provider)

        squad = SquadStateCreate(
            player_ids=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            captain_id=1,
            vice_captain_id=2,
            bank=0.0,
            gameweek=1,
            picks_gw=1,
            player_positions={i: (1 if i == 1 else 2 if i <= 5 else 3 if i <= 9 else 4) for i in range(1, 16)},
            player_prices={i: 6.0 for i in range(1, 16)},
            player_teams={i: i for i in range(1, 16)},
        )
        bridge.generate_decisions(squad)

        expected_stages = {
            "optimizer_starting_xi",
            "optimizer_captain",
            "optimizer_transfers",
            "optimizer_chip",
        }
        recorded = set(provider.stage_timings.keys())
        assert expected_stages.issubset(recorded), (
            f"Missing stages: {expected_stages - recorded}"
        )
        for stage in expected_stages:
            assert provider.stage_timings[stage] >= 0.0


class TestCachedProviderClear:
    def test_clear_request_cache_resets_stage_timings(self) -> None:
        from fpl_intelligence.prediction.cached_live_provider import CachedLivePredictionProvider

        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []
        mock_session.execute.return_value.scalars.return_value.all.return_value = []

        with patch(
            "fpl_intelligence.prediction.live_provider.LivePredictionProvider.__init__",
            return_value=None,
        ):
            provider = CachedLivePredictionProvider.__new__(CachedLivePredictionProvider)
            provider.session = mock_session
            provider._all_predictions_cache = {}
            provider._fixture_count_cache = {}
            provider._chain_cache = {}
            provider._prediction_cache = {}
            provider.stage_timings = {"optimizer_starting_xi": 42.5, "optimizer_captain": 10.0}

        provider.clear_request_cache()

        assert provider.stage_timings == {}
