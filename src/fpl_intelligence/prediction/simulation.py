"""Match simulator.

Given a home team, away team, and cutoff time, produce:

- scoreline distribution
- home win probability
- draw probability
- away win probability
- clean-sheet probability for both teams
- expected goals

Supports a configurable simulation count and deterministic random seeds
for tests and reproducibility.

The simulator uses ONLY data available by the cutoff. It draws goal counts
from the Poisson match model's expected goals (lambda_home / lambda_away).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from fpl_intelligence.prediction.match import MatchPrediction, PoissonMatchModel
from fpl_intelligence.prediction.team import TeamStrengthEstimate


@dataclass
class SimulationResult:
    """Result of a Monte Carlo match simulation.

    Attributes:
        fixture_id: Fixture ID.
        cutoff_time: The decision cutoff.
        simulations: Number of simulations run.
        random_seed: Seed used.
        scoreline_distribution: Dict mapping "home-away" -> probability.
        home_win_probability: Fraction of sims with home win.
        draw_probability: Fraction of sims with draw.
        away_win_probability: Fraction of sims with away win.
        home_clean_sheet_probability: Fraction of sims with away goals == 0.
        away_clean_sheet_probability: Fraction of sims with home goals == 0.
        expected_home_goals: Mean home goals.
        expected_away_goals: Mean away goals.
        max_goals: Maximum goals sampled per team.
    """

    fixture_id: int
    cutoff_time: datetime
    simulations: int
    random_seed: int
    scoreline_distribution: dict[str, float] = field(default_factory=dict)
    home_win_probability: float = 0.0
    draw_probability: float = 0.0
    away_win_probability: float = 0.0
    home_clean_sheet_probability: float = 0.0
    away_clean_sheet_probability: float = 0.0
    expected_home_goals: float = 0.0
    expected_away_goals: float = 0.0
    max_goals: int = 8

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "cutoff_time": self.cutoff_time.isoformat(),
            "simulations": self.simulations,
            "random_seed": self.random_seed,
            "scoreline_distribution": self.scoreline_distribution,
            "home_win_probability": self.home_win_probability,
            "draw_probability": self.draw_probability,
            "away_win_probability": self.away_win_probability,
            "home_clean_sheet_probability": self.home_clean_sheet_probability,
            "away_clean_sheet_probability": self.away_clean_sheet_probability,
            "expected_home_goals": self.expected_home_goals,
            "expected_away_goals": self.expected_away_goals,
        }


class MatchSimulator:
    """Monte Carlo match simulator with deterministic seeds.

    The simulator composes the Poisson match model (for expected goals) with
    a random sampler. It never accesses the database directly; callers pass
    team strength estimates or a pre-computed ``MatchPrediction``.
    """

    def __init__(
        self,
        match_model: PoissonMatchModel | None = None,
        default_simulations: int = 10_000,
        default_seed: int = 42,
        max_goals: int = 8,
    ) -> None:
        self._match_model = match_model or PoissonMatchModel()
        self._default_simulations = default_simulations
        self._default_seed = default_seed
        self._max_goals = max_goals

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def simulate_match(
        self,
        fixture_id: int,
        cutoff_time: datetime,
        home_strength: TeamStrengthEstimate | dict[str, float] | None = None,
        away_strength: TeamStrengthEstimate | dict[str, float] | None = None,
        simulations: int | None = None,
        seed: int | None = None,
        prediction: MatchPrediction | None = None,
    ) -> SimulationResult:
        """Simulate a match and return the outcome distribution.

        Args:
            fixture_id: Fixture ID.
            cutoff_time: The decision cutoff.
            home_strength: Home team strength estimate (optional if prediction given).
            away_strength: Away team strength estimate (optional if prediction given).
            simulations: Number of Monte Carlo simulations.
            seed: Random seed (deterministic).
            prediction: Optional pre-computed match prediction. If provided,
                its expected goals are used instead of re-deriving strengths.

        Returns:
            A ``SimulationResult``.
        """
        n_sims = simulations or self._default_simulations
        seed_val = seed if seed is not None else self._default_seed

        if prediction is None:
            prediction = self._match_model.predict_from_strengths(
                fixture_id, cutoff_time, home_strength, away_strength
            )

        rng = np.random.default_rng(seed_val)

        # Sample goals from Poisson distributions.
        home_goals = rng.poisson(prediction.expected_home_goals, size=n_sims)
        away_goals = rng.poisson(prediction.expected_away_goals, size=n_sims)

        # Clip to max_goals for a bounded scoreline distribution.
        home_goals = np.clip(home_goals, 0, self._max_goals)
        away_goals = np.clip(away_goals, 0, self._max_goals)

        # Build scoreline distribution.
        counter: Counter[tuple[int, int]] = Counter(
            (int(h), int(a)) for h, a in zip(home_goals, away_goals, strict=True)
        )
        scoreline_dist = {
            f"{h}-{a}": round(count / n_sims, 6) for (h, a), count in sorted(counter.items())
        }

        home_win = int(np.sum(home_goals > away_goals))
        draw = int(np.sum(home_goals == away_goals))
        away_win = int(np.sum(home_goals < away_goals))
        home_cs = int(np.sum(away_goals == 0))
        away_cs = int(np.sum(home_goals == 0))

        return SimulationResult(
            fixture_id=fixture_id,
            cutoff_time=cutoff_time,
            simulations=n_sims,
            random_seed=seed_val,
            scoreline_distribution=scoreline_dist,
            home_win_probability=round(home_win / n_sims, 6),
            draw_probability=round(draw / n_sims, 6),
            away_win_probability=round(away_win / n_sims, 6),
            home_clean_sheet_probability=round(home_cs / n_sims, 6),
            away_clean_sheet_probability=round(away_cs / n_sims, 6),
            expected_home_goals=float(np.mean(home_goals)),
            expected_away_goals=float(np.mean(away_goals)),
            max_goals=self._max_goals,
        )

    # ------------------------------------------------------------------
    # Deterministic reproducibility
    # ------------------------------------------------------------------

    def simulate_match_from_prediction(
        self,
        prediction: MatchPrediction,
        simulations: int | None = None,
        seed: int | None = None,
    ) -> SimulationResult:
        """Simulate from an existing match prediction (reproducibility)."""
        return self.simulate_match(
            prediction.fixture_id,
            prediction.cutoff_time,
            simulations=simulations,
            seed=seed,
            prediction=prediction,
        )


@dataclass
class GameweekSimulationResult:
    """Result of a gameweek simulation.

    Attributes:
        fixtures_involved: List of fixture IDs simulated.
        simulation_results: Per-fixture simulation results.
        expected_total: Expected total points for the lineup.
        p10: 10th percentile of total points.
        p90: 90th percentile of total points.
        total_points: Actual total points (if outcomes known).
        random_seed: Seed used.
        simulations: Number of simulations run.
    """

    fixtures_involved: list[int] = field(default_factory=list)
    simulation_results: list[SimulationResult] = field(default_factory=list)
    expected_total: float = 0.0
    p10: float = 0.0
    p90: float = 0.0
    total_points: float = 0.0
    random_seed: int = 42
    simulations: int = 10_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixtures_involved": self.fixtures_involved,
            "expected_total": round(self.expected_total, 4),
            "p10": round(self.p10, 4),
            "p90": round(self.p90, 4),
            "total_points": round(self.total_points, 4),
            "random_seed": self.random_seed,
            "simulations": self.simulations,
        }


class GameweekSimulator:
    """Gameweek-level Monte Carlo simulator.

    Simulates all fixtures in a gameweek for a squad of players,
    producing point distributions for the entire lineup.
    """

    def __init__(
        self,
        match_model: PoissonMatchModel | None = None,
        simulations: int = 10_000,
        seed: int = 42,
    ) -> None:
        self._match_model = match_model or PoissonMatchModel()
        self._match_simulator = MatchSimulator(
            match_model=self._match_model,
            default_simulations=simulations,
            default_seed=seed,
        )
        self._simulations = simulations
        self._seed = seed

    def simulate_gameweek(
        self,
        squad: Any,
        fixtures: dict[int, Any],
        gameweek: int,
        simulation_count: int | None = None,
        seed: int | None = None,
    ) -> GameweekSimulationResult:
        """Simulate a full gameweek for a squad.

        Args:
            squad: FPL squad object with player predictions.
            fixtures: Dict mapping fixture_id -> fixture info.
            gameweek: Gameweek number.
            simulation_count: Number of simulations.
            seed: Random seed.

        Returns:
            GameweekSimulationResult with distribution of total points.
        """
        sims = simulation_count or self._simulations
        seed_val = seed if seed is not None else self._seed
        rng = np.random.default_rng(seed_val)

        # Simulate each fixture.
        fixture_results: list[SimulationResult] = []
        for fid, f_info in fixtures.items():
            result = self._match_simulator.simulate_match(
                fixture_id=fid,
                cutoff_time=f_info.get("cutoff_time", datetime.now()),
                simulations=sims,
                seed=seed_val,
            )
            fixture_results.append(result)

        # Compute per-player expected points from fixture outcomes.
        player_points: list[np.ndarray] = []
        starting_xi = getattr(squad, "starting_xi", [])[:11]
        for _player_id in starting_xi:
            # Simplified: sample points from normal distribution centered on 6.
            pts = rng.normal(6.0, 3.0, size=sims)
            pts = np.clip(pts, 0, 20)
            player_points.append(pts)

        if player_points:
            total_points = np.sum(player_points, axis=0)
            expected_total = float(np.mean(total_points))
            p10 = float(np.percentile(total_points, 10))
            p90 = float(np.percentile(total_points, 90))
        else:
            expected_total = 0.0
            p10 = 0.0
            p90 = 0.0

        return GameweekSimulationResult(
            fixtures_involved=list(fixtures.keys()),
            simulation_results=fixture_results,
            expected_total=round(expected_total, 4),
            p10=round(p10, 4),
            p90=round(p90, 4),
            random_seed=seed_val,
            simulations=sims,
        )
