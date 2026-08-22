"""Advanced player model orchestrator for Phase 5."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from fpl_intelligence.prediction.advanced_player.assist_model import AssistModel, AssistPrediction
from fpl_intelligence.prediction.advanced_player.bonus_model import BonusModel
from fpl_intelligence.prediction.advanced_player.clean_sheet_model import (
    CleanSheetModel,
    CleanSheetPrediction,
)
from fpl_intelligence.prediction.advanced_player.defensive_contribution_model import (
    DefensiveContributionModel,
    DefensiveContributionPrediction,
)
from fpl_intelligence.prediction.advanced_player.goal_model import GoalModel, GoalPrediction
from fpl_intelligence.prediction.minutes import MinutesModel
from fpl_intelligence.prediction.scoring import FPLPointsComponents, FPLScoringEngine


@dataclass
class AdvancedPlayerOutput:
    """Full advanced player prediction output."""

    player_id: int
    fixture_id: int
    cutoff_time: datetime
    expected_minutes: float = 0.0
    probability_starting: float = 0.0
    probability_30_plus: float = 0.0
    probability_60_plus: float = 0.0
    expected_points: float = 0.0
    points_p10: float = 0.0
    points_p25: float = 0.0
    points_p50: float = 0.0
    points_p75: float = 0.0
    points_p90: float = 0.0
    probability_2_plus: float = 0.0
    probability_5_plus: float = 0.0
    probability_10_plus: float = 0.0
    probability_15_plus: float = 0.0
    floor: float = 0.0
    ceiling: float = 0.0
    goal_prediction: GoalPrediction | None = None
    assist_prediction: AssistPrediction | None = None
    clean_sheet_prediction: CleanSheetPrediction | None = None
    bonus_prediction: Any | None = None
    defensive_prediction: DefensiveContributionPrediction | None = None
    components: FPLPointsComponents = field(default_factory=FPLPointsComponents)
    uncertainty: dict[str, str] = field(default_factory=dict)
    data_completeness: float = 0.0
    method: str = "advanced_player_model_v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "fixture_id": self.fixture_id,
            "cutoff_time": self.cutoff_time.isoformat(),
            "expected_minutes": round(self.expected_minutes, 4),
            "probability_starting": round(self.probability_starting, 4),
            "probability_30_plus": round(self.probability_30_plus, 4),
            "probability_60_plus": round(self.probability_60_plus, 4),
            "expected_points": round(self.expected_points, 4),
            "points_p10": round(self.points_p10, 4),
            "points_p25": round(self.points_p25, 4),
            "points_p50": round(self.points_p50, 4),
            "points_p75": round(self.points_p75, 4),
            "points_p90": round(self.points_p90, 4),
            "probability_2_plus": round(self.probability_2_plus, 4),
            "probability_5_plus": round(self.probability_5_plus, 4),
            "probability_10_plus": round(self.probability_10_plus, 4),
            "probability_15_plus": round(self.probability_15_plus, 4),
            "floor": round(self.floor, 4),
            "ceiling": round(self.ceiling, 4),
            "components": self.components.to_dict(),
            "uncertainty": self.uncertainty,
            "data_completeness": round(self.data_completeness, 4),
            "method": self.method,
        }


class AdvancedPlayerModel:
    """Advanced player prediction model for Phase 5."""

    def __init__(
        self,
        minutes_model: MinutesModel | None = None,
        scoring_engine: FPLScoringEngine | None = None,
        simulation_count: int = 10_000,
        default_seed: int = 42,
    ) -> None:
        self._minutes_model = minutes_model
        self._scoring_engine = scoring_engine or FPLScoringEngine()
        self._simulation_count = simulation_count
        self._default_seed = default_seed

        self._goal_model = GoalModel()
        self._assist_model = AssistModel()
        self._clean_sheet_model = CleanSheetModel()
        self._bonus_model = BonusModel()
        self._defensive_model = DefensiveContributionModel()

    def _get_minutes_prediction(
        self, features: dict[str, float], context: dict[str, Any]
    ) -> dict[str, float]:
        """Get minutes prediction from features."""
        return {
            "expected_minutes": features.get("expected_minutes", 60.0),
            "probability_starting": features.get("probability_starting", 0.5),
            "probability_30_plus": features.get("probability_30_plus", 0.5),
            "probability_60_plus": features.get("probability_60_plus", 0.5),
        }

    def _simulate_points(
        self,
        components: FPLPointsComponents,
        position_code: int,
        seed: int = 42,
    ) -> dict[str, float]:
        """Monte Carlo simulation to derive full point distribution."""
        rng = np.random.default_rng(seed)
        n = self._simulation_count

        goals = rng.poisson(components.expected_goals, size=n)
        assists = rng.poisson(components.expected_assists, size=n)
        minutes = np.clip(
            rng.normal(
                components.appearance_minutes,
                max(5.0, components.appearance_minutes * 0.3),
                size=n,
            ),
            0,
            90,
        )
        clean_sheet = rng.random(n) < components.expected_clean_sheet
        bonus_prob = min(1.0, components.expected_bonus / 2.0)
        bonus = rng.random(n) < bonus_prob
        bonus_pts = bonus.astype(float) * rng.choice([1, 2, 3], size=n, p=[0.2, 0.5, 0.3])
        def_prob = min(1.0, components.defensive_contribution)
        def_contrib = (rng.random(n) < def_prob).astype(float)

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
            result = self._scoring_engine.compute(comp, position_code)
            points[i] = result["total"]

        return {
            "expected": round(float(np.mean(points)), 4),
            "p10": round(float(np.percentile(points, 10)), 4),
            "p25": round(float(np.percentile(points, 25)), 4),
            "p50": round(float(np.percentile(points, 50)), 4),
            "p75": round(float(np.percentile(points, 75)), 4),
            "p90": round(float(np.percentile(points, 90)), 4),
            "p_2_plus": round(float(np.mean(points >= 2)), 4),
            "p_5_plus": round(float(np.mean(points >= 5)), 4),
            "p_10_plus": round(float(np.mean(points >= 10)), 4),
            "p_15_plus": round(float(np.mean(points >= 15)), 4),
            "floor": round(float(np.percentile(points, 5)), 4),
            "ceiling": round(float(np.percentile(points, 95)), 4),
        }

    def _decompose_uncertainty(
        self,
        minutes_ctx: dict[str, float],
        goal_pred: GoalPrediction,
        assist_pred: AssistPrediction,
        context: dict[str, Any],
    ) -> dict[str, str]:
        """Decompose uncertainty by source."""
        minutes_unc = "high" if minutes_ctx.get("probability_starting", 0.5) < 0.7 else "low"
        goal_unc = (
            "high"
            if goal_pred.expected_goals < 0.2
            else "medium"
            if goal_pred.expected_goals < 0.5
            else "low"
        )
        assist_unc = (
            "high"
            if assist_pred.expected_assists < 0.1
            else "medium"
            if assist_pred.expected_assists < 0.3
            else "low"
        )
        return {
            "minutes_uncertainty": minutes_unc,
            "performance_uncertainty": goal_unc,
            "match_uncertainty": "medium",
            "assist_uncertainty": assist_unc,
        }

    def _compute_completeness(
        self,
        goal_pred: GoalPrediction,
        assist_pred: AssistPrediction,
        cs_pred: CleanSheetPrediction,
        bonus_pred: Any,
        def_pred: DefensiveContributionPrediction,
    ) -> float:
        """Compute overall data completeness."""
        scores = [
            goal_pred.data_completeness,
            assist_pred.data_completeness,
            cs_pred.data_completeness,
            bonus_pred.data_completeness if bonus_pred.available else 0.5,
            def_pred.data_completeness if def_pred.available else 0.5,
        ]
        return round(sum(scores) / len(scores), 4) if scores else 0.0

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        cutoff_time: datetime | None = None,
        context: dict[str, Any] | None = None,
    ) -> AdvancedPlayerOutput:
        """Full advanced player prediction pipeline."""
        context = context or {}
        ct = cutoff_time or datetime.now()

        # 1. Minutes
        minutes_pred = self._get_minutes_prediction(features, context)
        expected_minutes = minutes_pred.get("expected_minutes", 60.0)
        prob_starting = minutes_pred.get("probability_starting", 0.5)
        prob_30_plus = minutes_pred.get("probability_30_plus", 0.5)
        prob_60_plus = minutes_pred.get("probability_60_plus", 0.5)

        # 2. Goal distribution
        goal_pred = self._goal_model.predict(player_id, fixture_id, features, context)

        # 3. Assist distribution
        assist_pred = self._assist_model.predict(player_id, fixture_id, features, context)

        # 4. Clean sheet
        cs_pred = self._clean_sheet_model.predict(player_id, fixture_id, features, context)

        # 5. Bonus
        bonus_pred = self._bonus_model.predict(player_id, fixture_id, features, context)

        # 6. Defensive contribution
        def_pred = self._defensive_model.predict(player_id, fixture_id, features, context)

        # 7. Assemble FPL scoring components
        components = FPLPointsComponents(
            expected_goals=goal_pred.expected_goals,
            expected_assists=assist_pred.expected_assists,
            expected_clean_sheet=cs_pred.joint_probability,
            expected_bonus=bonus_pred.expected_bonus_points if bonus_pred.available else 0.0,
            appearance_minutes=expected_minutes,
            defensive_contribution=def_pred.expected_points if def_pred.available else 0.0,
        )

        # 8. Compute expected points via scoring engine
        pts = self._scoring_engine.expected_points(
            components, int(features.get("position_code", 3))
        )
        expected_points = pts["total"]

        # 9. Monte Carlo distribution
        position_code = int(features.get("position_code", 3))
        sim_result = self._simulate_points(components, position_code, seed=self._default_seed)

        # 10. Uncertainty decomposition
        uncertainty = self._decompose_uncertainty(minutes_pred, goal_pred, assist_pred, context)

        # 11. Data completeness
        completeness = self._compute_completeness(
            goal_pred, assist_pred, cs_pred, bonus_pred, def_pred
        )

        return AdvancedPlayerOutput(
            player_id=player_id,
            fixture_id=fixture_id,
            cutoff_time=ct,
            expected_minutes=round(expected_minutes, 4),
            probability_starting=round(prob_starting, 4),
            probability_30_plus=round(prob_30_plus, 4),
            probability_60_plus=round(prob_60_plus, 4),
            expected_points=round(expected_points, 4),
            points_p10=sim_result["p10"],
            points_p25=sim_result["p25"],
            points_p50=sim_result["p50"],
            points_p75=sim_result["p75"],
            points_p90=sim_result["p90"],
            probability_2_plus=sim_result["p_2_plus"],
            probability_5_plus=sim_result["p_5_plus"],
            probability_10_plus=sim_result["p_10_plus"],
            probability_15_plus=sim_result["p_15_plus"],
            floor=sim_result["floor"],
            ceiling=sim_result["ceiling"],
            goal_prediction=goal_pred,
            assist_prediction=assist_pred,
            clean_sheet_prediction=cs_pred,
            bonus_prediction=bonus_pred,
            defensive_prediction=def_pred,
            components=components,
            uncertainty=uncertainty,
            data_completeness=completeness,
            method="advanced_player_model_v1",
        )
