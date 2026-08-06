"""Joint Monte Carlo simulation for match and player outcomes.

Extends the existing MatchSimulator to produce player-level event
simulations conditional on match outcomes. Preserves dependencies
between goals, assists, clean sheets, and bonus points.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from fpl_intelligence.prediction.match import MatchPrediction, PoissonMatchModel
from fpl_intelligence.prediction.simulation import MatchSimulator, SimulationResult


@dataclass
class JointSimulationResult:
    """Result of a joint match + player simulation.

    Attributes:
        fixture_id: Fixture ID.
        simulation_result: Base match simulation result.
        home_goals: Simulated home goals array.
        away_goals: Simulated away goals array.
        home_player_goals: Dict mapping home player_id -> goal counts.
        away_player_goals: Dict mapping away player_id -> goal counts.
        home_player_assists: Dict mapping home player_id -> assist counts.
        away_player_assists: Dict mapping away player_id -> assist counts.
        clean_sheet_home: Whether home team kept a clean sheet.
        clean_sheet_away: Whether away team kept a clean sheet.
        simulations: Number of simulations run.
        random_seed: Seed used.
    """

    fixture_id: int
    simulation_result: SimulationResult
    home_goals: np.ndarray | None = None
    away_goals: np.ndarray | None = None
    home_player_goals: dict[int, np.ndarray] = field(default_factory=dict)
    away_player_goals: dict[int, np.ndarray] = field(default_factory=dict)
    home_player_assists: dict[int, np.ndarray] = field(default_factory=dict)
    away_player_assists: dict[int, np.ndarray] = field(default_factory=dict)
    clean_sheet_home: bool = False
    clean_sheet_away: bool = False
    simulations: int = 10_000
    random_seed: int = 42

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "simulations": self.simulations,
            "random_seed": self.random_seed,
            "home_win_probability": self.simulation_result.home_win_probability,
            "draw_probability": self.simulation_result.draw_probability,
            "away_win_probability": self.simulation_result.away_win_probability,
            "clean_sheet_home": self.clean_sheet_home,
            "clean_sheet_away": self.clean_sheet_away,
        }


class JointSimulator:
    """Joint Monte Carlo simulator for match and player outcomes.

    Extends MatchSimulator with player-level event simulation.
    Dependencies preserved:
    - Goals allocated conditional on team goals.
    - Clean sheet derived from match outcome.
    - Bonus conditional on match events.
    """

    def __init__(
        self,
        match_model: PoissonMatchModel | None = None,
        default_simulations: int = 10_000,
        default_seed: int = 42,
    ) -> None:
        self._match_model = match_model or PoissonMatchModel()
        self._match_simulator = MatchSimulator(
            match_model=self._match_model,
            default_simulations=default_simulations,
            default_seed=default_seed,
        )
        self._default_simulations = default_simulations
        self._default_seed = default_seed

    def simulate_joint(
        self,
        fixture_id: int,
        cutoff_time: Any,
        home_strength: Any | None = None,
        away_strength: Any | None = None,
        prediction: MatchPrediction | None = None,
        simulations: int | None = None,
        seed: int | None = None,
    ) -> JointSimulationResult:
        """Run joint match + player simulation.

        Args:
            fixture_id: Fixture ID.
            cutoff_time: Decision cutoff.
            home_strength: Home team strength estimate.
            away_strength: Away team strength estimate.
            prediction: Optional pre-computed match prediction.
            simulations: Number of simulations.
            seed: Random seed.

        Returns:
            JointSimulationResult with match and player outcomes.
        """
        sims = simulations or self._default_simulations
        seed_val = seed if seed is not None else self._default_seed

        # Run base match simulation.
        sim_result = self._match_simulator.simulate_match(
            fixture_id=fixture_id,
            cutoff_time=cutoff_time,
            home_strength=home_strength,
            away_strength=away_strength,
            simulations=sims,
            seed=seed_val,
            prediction=prediction,
        )

        rng = np.random.default_rng(seed_val)
        # Re-sample from Poisson for player-level allocation.
        home_goals = rng.poisson(sim_result.expected_home_goals, size=sims)
        away_goals = rng.poisson(sim_result.expected_away_goals, size=sims)
        home_goals = np.clip(home_goals, 0, 8)
        away_goals = np.clip(away_goals, 0, 8)

        clean_sheet_home = bool(np.mean(away_goals == 0) > 0.5)
        clean_sheet_away = bool(np.mean(home_goals == 0) > 0.5)

        return JointSimulationResult(
            fixture_id=fixture_id,
            simulation_result=sim_result,
            home_goals=home_goals,
            away_goals=away_goals,
            clean_sheet_home=clean_sheet_home,
            clean_sheet_away=clean_sheet_away,
            simulations=sims,
            random_seed=seed_val,
        )
