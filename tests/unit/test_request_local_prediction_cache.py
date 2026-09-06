"""Regression tests for request-local prediction cache semantics."""

from __future__ import annotations

import numpy as np

from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.squad.bridge import _TimedPredictionProvider


def _prediction(player_id: int, gameweek: int, *, full: bool) -> PlayerPrediction:
    distribution = np.array([1.0, 2.0, 3.0]) if full else np.empty(0)
    return PlayerPrediction(
        player_id=player_id,
        gameweek=gameweek,
        expected_points=2.0,
        expected_minutes=90.0,
        start_probability=1.0,
        distribution=distribution,
        floor=1.0,
        ceiling=3.0,
    )


class RecordingProvider:
    """Minimal provider that exposes separate bulk and full prediction calls."""

    def __init__(self) -> None:
        self.bulk_calls = 0
        self.full_calls = 0

    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        self.full_calls += 1
        return _prediction(player_id, gameweek, full=True)

    def get_squad_predictions(
        self, squad_players: list[int], gameweeks: list[int]
    ) -> dict[int, dict[int, PlayerPrediction]]:
        self.bulk_calls += 1
        return {
            int(gameweeks[0]): {
                int(pid): _prediction(int(pid), int(gameweeks[0]), full=False)
                for pid in squad_players
            }
        }

    def get_all_predictions(self, gameweek: int) -> dict[int, PlayerPrediction]:
        return {1: _prediction(1, gameweek, full=False)}

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        return 1


def test_distributionless_bulk_predictions_are_not_promoted_to_full_cache() -> None:
    provider = RecordingProvider()
    timed = _TimedPredictionProvider(provider)  # type: ignore[arg-type]

    bulk = timed.get_squad_predictions([1], [5])
    assert bulk[5][1].distribution.size == 0
    assert provider.bulk_calls == 1

    full = timed.get_player_prediction(1, 5)
    assert full.distribution.size == 3
    assert provider.full_calls == 1

    # Once the full object exists, repeated single-player requests are local hits.
    assert timed.get_player_prediction(1, 5) is full
    assert provider.full_calls == 1


def test_full_bulk_predictions_are_reused_by_single_player_requests() -> None:
    provider = RecordingProvider()
    timed = _TimedPredictionProvider(provider)  # type: ignore[arg-type]

    full = _prediction(2, 6, full=True)

    def full_bulk(
        squad_players: list[int], gameweeks: list[int]
    ) -> dict[int, dict[int, PlayerPrediction]]:
        return {int(gameweeks[0]): {int(squad_players[0]): full}}

    provider.get_squad_predictions = full_bulk  # type: ignore[method-assign]

    bulk = timed.get_squad_predictions([2], [6])
    assert bulk[6][2] is full
    assert timed.get_player_prediction(2, 6) is full
    assert provider.full_calls == 0
