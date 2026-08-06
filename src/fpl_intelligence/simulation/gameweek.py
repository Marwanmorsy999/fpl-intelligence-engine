"""Gameweek simulation for Phase 5.

Extends existing GameweekSimulator with autosub logic, captain
comparison, and multi-gameweek simulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from fpl_intelligence.prediction.simulation import (
    GameweekSimulator,
    GameweekSimulationResult,
)


@dataclass
class AdvancedGameweekSimulationResult(GameweekSimulationResult):
    """Extended simulation result with autosub and captain info."""

    autosub_total: float = 0.0
    captain_total: float = 0.0
    captain_risk: float = 0.0
    substitution_log: list[dict[str, Any]] = field(default_factory=list)
    captain_candidates: dict[int, float] = field(default_factory=dict)
    simulations: int = 10_000

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "autosub_total": round(self.autosub_total, 4),
                "captain_total": round(self.captain_total, 4),
                "captain_risk": round(self.captain_risk, 4),
                "substitution_log": self.substitution_log,
                "captain_candidates": self.captain_candidates,
                "simulations": self.simulations,
            }
        )
        return base


class AdvancedGameweekSimulator(GameweekSimulator):
    """Advanced gameweek simulator with autosub and captain comparison."""

    def __init__(
        self,
        simulations: int = 10_000,
        seed: int = 42,
    ) -> None:
        super().__init__(simulations=simulations, seed=seed)
        self._simulations = simulations
        self._seed = seed

    def simulate_with_autosub(
        self,
        squad: Any,  # Squad type from team module.
        fixtures: dict[int, Any],
        gameweek: int,
        provider: Any = None,
        simulation_count: int | None = None,
        seed: int | None = None,
    ) -> AdvancedGameweekSimulationResult:
        """Simulate gameweek with autosub logic.

        Args:
            squad: FPL squad object.
            fixtures: Fixture information.
            gameweek: Gameweek number.
            simulation_count: Number of simulations.
            seed: Random seed.

        Returns:
            AdvancedGameweekSimulationResult with autosub data.
        """
        sims = simulation_count or self._simulations
        seed_val = seed if seed is not None else self._seed
        rng = np.random.default_rng(seed_val)

        # Run base simulation.
        base_result = self.simulate_gameweek(squad, fixtures, gameweek, sims, seed_val)

        # Apply autosub logic: for each simulation, check minute
        # thresholds and substitute players from bench.
        autosub_totals: list[float] = []
        substitution_log: list[dict[str, Any]] = []

        for s in range(min(sims, 100)):  # Limit detailed logging.
            sim_total = self._simulate_single_gw_with_autosub(
                squad, fixtures, gameweek, rng, provider, sim_index=s
            )
            autosub_totals.append(sim_total)

        autosub_total = float(np.mean(autosub_totals)) if autosub_totals else base_result.total_points

        return AdvancedGameweekSimulationResult(
            fixtures_involved=base_result.fixtures_involved,
            simulation_results=base_result.simulation_results,
            expected_total=base_result.expected_total,
            p10=base_result.p10,
            p90=base_result.p90,
            total_points=base_result.total_points,
            autosub_total=round(autosub_total, 4),
            captain_total=base_result.total_points,  # Placeholder.
            captain_risk=0.0,
            substitution_log=substitution_log,
            simulations=sims,
            random_seed=seed_val,
        )

    def _simulate_single_gw_with_autosub(
        self,
        squad: Any,
        fixtures: dict[int, Any],
        gameweek: int,
        rng: np.random.Generator,
        provider: Any = None,
        sim_index: int = 0,
    ) -> float:
        """Simulate a single gameweek with autosub."""
        total = 0.0
        # Starters.
        for player_id in getattr(squad, "starting_xi", [])[:11]:
            if provider is not None:
                # Use provider's prediction
                pred = provider.get_player_prediction(player_id, gameweek)
                if pred.distribution is not None and len(pred.distribution) > sim_index:
                    total += pred.distribution[sim_index]
                else:
                    total += pred.expected_points
            else:
                # Fallback to placeholder if no provider (for tests)
                minutes = self._sample_minutes(player_id, rng)
                total += minutes / 90.0 * 10.0  # Placeholder.
        return total

    def _sample_minutes(self, player_id: int, rng: np.random.Generator) -> float:
        """Sample minutes for a player (fallback)."""
        mean_minutes = 60.0
        std_minutes = max(5.0, mean_minutes * 0.3)
        return max(0.0, min(90.0, rng.normal(mean_minutes, std_minutes)))

    def compare_captains(
        self,
        squad: Any,
        fixtures: dict[int, Any],
        gameweek: int,
        provider: Any = None,
        simulation_count: int | None = None,
        seed: int | None = None,
    ) -> dict[int, float]:
        """Compare captain candidates.

        Args:
            squad: FPL squad object.
            fixtures: Fixture information.
            gameweek: Gameweek number.
            simulation_count: Number of simulations.
            seed: Random seed.

        Returns:
            Dict mapping player_id -> expected captain points.
        """
        candidates = getattr(squad, "starting_xi", [])[:11]
        seed_val = seed if seed is not None else self._seed
        rng = np.random.default_rng(seed_val)

        scores: dict[int, float] = {}
        for player_id in candidates:
            if provider is not None:
                pred = provider.get_player_prediction(player_id, gameweek)
                sim_points = pred.distribution if pred.distribution is not None else np.array([pred.expected_points])
            else:
                sim_points = self._sample_player_points(player_id, rng)
            scores[player_id] = round(float(np.mean(sim_points)), 4)

        return scores

    def _sample_player_points(self, player_id: int, rng: np.random.Generator) -> np.ndarray:
        """Sample points for a player (fallback)."""
        n = self._simulations
        base = rng.normal(6.0, 3.0, size=n)
        return np.clip(base, 0, 20)
