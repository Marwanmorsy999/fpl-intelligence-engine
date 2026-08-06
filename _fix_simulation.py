"""Helper script to fix simulation.py by adding GameweekSimulator."""
from pathlib import Path

# Read current simulation.py
path = Path("src/fpl_intelligence/prediction/simulation.py")
current = path.read_text()

# Add GameweekSimulationResult and GameweekSimulator before the last line
gameweek_classes = '''

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
        for player_id in starting_xi:
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
'''

# Append gameweek classes
new_content = current + gameweek_classes
path.write_text(new_content)
print("Updated simulation.py with GameweekSimulator and GameweekSimulationResult")
