"""Rank and rival strategies."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any


@dataclass
class RivalBenchmark:
    """A benchmark squad (e.g. template, mini-league rival)."""

    benchmark_id: str
    squad_players: list[int]
    captain: int
    expected_points: float = 0.0


class RankStrategy(abc.ABC):
    """Abstract strategy for rank-based optimization."""

    @abc.abstractmethod
    def evaluate_action_vs_benchmark(
        self,
        base_ev: float,
        benchmark: RivalBenchmark,
        action_players: list[int],
    ) -> float:
        """Adjust EV based on strategy against benchmark."""
        pass


class ProtectRankStrategy(RankStrategy):
    """Minimizes differential risk against a benchmark."""

    def evaluate_action_vs_benchmark(
        self,
        base_ev: float,
        benchmark: RivalBenchmark,
        action_players: list[int],
    ) -> float:
        """Increase value of players owned by the benchmark."""
        overlap = len(set(action_players).intersection(set(benchmark.squad_players)))
        # Slight bump to EV for overlapping players to encourage defensive picks
        return base_ev * (1.0 + (overlap * 0.02))


class ChaseRankStrategy(RankStrategy):
    """Maximizes differential upside against a benchmark."""

    def evaluate_action_vs_benchmark(
        self,
        base_ev: float,
        benchmark: RivalBenchmark,
        action_players: list[int],
    ) -> float:
        """Increase value of differential players not owned by the benchmark."""
        differentials = len(set(action_players) - set(benchmark.squad_players))
        # Slight bump to EV for differential players
        return base_ev * (1.0 + (differentials * 0.05))
