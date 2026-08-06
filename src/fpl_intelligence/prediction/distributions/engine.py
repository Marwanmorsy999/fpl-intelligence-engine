"""Point distribution engine for Phase 5."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class PointDistribution:
    """Full point distribution for a prediction."""

    expected_points: float = 0.0
    p10: float = 0.0
    p25: float = 0.0
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    p_2_plus: float = 0.0
    p_5_plus: float = 0.0
    p_10_plus: float = 0.0
    p_15_plus: float = 0.0
    floor: float = 0.0
    ceiling: float = 0.0
    samples: np.ndarray | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_points": round(self.expected_points, 4),
            "p10": round(self.p10, 4),
            "p25": round(self.p25, 4),
            "p50": round(self.p50, 4),
            "p75": round(self.p75, 4),
            "p90": round(self.p90, 4),
            "p_2_plus": round(self.p_2_plus, 4),
            "p_5_plus": round(self.p_5_plus, 4),
            "p_10_plus": round(self.p_10_plus, 4),
            "p_15_plus": round(self.p_15_plus, 4),
            "floor": round(self.floor, 4),
            "ceiling": round(self.ceiling, 4),
        }


class DistributionEngine:
    """Engine for computing point distributions from component probabilities."""

    def __init__(self, simulation_count: int = 10_000, default_seed: int = 42) -> None:
        self._simulation_count = simulation_count
        self._default_seed = default_seed
    def compute_distribution(
        self,
        components: dict[str, float],
        position_code: int = 3,
        seed: int | None = None,
    ) -> PointDistribution:
        """Compute full point distribution from component expectations."""
        seed = seed if seed is not None else self._default_seed
        rng = np.random.default_rng(seed)
        n = self._simulation_count

        goals = rng.poisson(components.get("expected_goals", 0.0), size=n)
        assists = rng.poisson(components.get("expected_assists", 0.0), size=n)
        minutes = np.clip(
            rng.normal(
                components.get("appearance_minutes", 60.0),
                max(5.0, components.get("appearance_minutes", 60.0) * 0.3),
                size=n,
            ),
            0, 90,
        )
        clean_sheet = rng.random(n) < components.get("expected_clean_sheet", 0.0)
        bonus_prob = min(1.0, components.get("expected_bonus", 0.0) / 2.0)
        bonus = rng.random(n) < bonus_prob
        bonus_pts = bonus.astype(float) * rng.choice([1, 2, 3], size=n, p=[0.2, 0.5, 0.3])
        def_prob = min(1.0, components.get("defensive_contribution", 0.0))
        def_contrib = (rng.random(n) < def_prob).astype(float)

        from fpl_intelligence.prediction.scoring import FPLPointsComponents, FPLScoringEngine
        engine = FPLScoringEngine()

        points = np.zeros(n)
        for i in range(n):
            comp = FPLPointsComponents(
                expected_goals=float(goals[i]),
                expected_assists=float(assists[i]),
                expected_clean_sheet=float(clean_sheet[i]),
                expected_bonus=float(bonus_pts[i]),
                appearance_minutes=float(minutes[i]),
                defensive_contribution=float(def_contrib[i]),
            )
            result = engine.compute(comp, position_code)
            points[i] = result["total"]

        return PointDistribution(
            expected_points=round(float(np.mean(points)), 4),
            p10=round(float(np.percentile(points, 10)), 4),
            p25=round(float(np.percentile(points, 25)), 4),
            p50=round(float(np.percentile(points, 50)), 4),
            p75=round(float(np.percentile(points, 75)), 4),
            p90=round(float(np.percentile(points, 90)), 4),
            p_2_plus=round(float(np.mean(points >= 2)), 4),
            p_5_plus=round(float(np.mean(points >= 5)), 4),
            p_10_plus=round(float(np.mean(points >= 10)), 4),
            p_15_plus=round(float(np.mean(points >= 15)), 4),
            floor=round(float(np.percentile(points, 5)), 4),
            ceiling=round(float(np.percentile(points, 95)), 4),
            samples=points,
        )

