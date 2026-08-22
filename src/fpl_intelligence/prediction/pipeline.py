"""Player baseline prediction pipeline.

Connects:

.. code-block:: text

    MinutesModel
    +
    TeamStrengthModel
    +
    PoissonMatchModel
    +
    FPLScoringEngine
    +
    simple player form
    =
    Baseline expected FPL points

The pipeline is modular: each component can be replaced independently.
The advanced player model (Phase 5) will replace this entire pipeline
by implementing the same ``PredictionModel`` protocol.

Expected-points output supports uncertainty via:
- expected value (E[x])
- lower estimate (5th percentile)
- upper estimate (95th percentile)

Where full distributions are not yet possible, the approximation is
documented in the output metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fpl_intelligence.prediction.baselines import (
    FixtureAdjustedBaselineModel,
)
from fpl_intelligence.prediction.match import PoissonMatchModel
from fpl_intelligence.prediction.minutes import MinutesModel
from fpl_intelligence.prediction.scoring import FPLPointsComponents, FPLScoringEngine
from fpl_intelligence.prediction.simulation import MatchSimulator
from fpl_intelligence.prediction.team import TeamStrengthModel


@dataclass
class PlayerBaselineOutput:
    """Expected FPL points for a player in an upcoming fixture.

    Attributes:
        player_id: Player ID.
        fixture_id: Fixture ID.
        cutoff_time: The decision cutoff.
        expected_minutes: Model-estimated expected minutes.
        probability_starting: P(start).
        expected_points: Expected FPL points (E[x]).
        points_lower: Lower bound (5th percentile approximation).
        points_upper: Upper bound (95th percentile approximation).
        components: Decomposed FPL point components.
        data_completeness: 0-1 completeness estimate.
        method: Description of the prediction method.
    """

    player_id: int
    fixture_id: int
    cutoff_time: datetime
    expected_minutes: float = 0.0
    probability_starting: float = 0.0
    expected_points: float = 0.0
    points_lower: float = 0.0
    points_upper: float = 0.0
    components: FPLPointsComponents = field(default_factory=FPLPointsComponents)
    data_completeness: float = 0.0
    method: str = "baseline_pipeline"

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "fixture_id": self.fixture_id,
            "cutoff_time": self.cutoff_time.isoformat(),
            "expected_minutes": self.expected_minutes,
            "probability_starting": self.probability_starting,
            "expected_points": round(self.expected_points, 4),
            "points_lower": round(self.points_lower, 4),
            "points_upper": round(self.points_upper, 4),
            "components": self.components.to_dict(),
            "data_completeness": self.data_completeness,
            "method": self.method,
        }


class PlayerBaselinePipeline:
    """Connects minutes, team/match, form, and FPL scoring.

    This is a first-pass pipeline. Each component can be upgraded:

    - ``MinutesModel`` → advanced player model (Phase 5).
    - ``FixtureAdjustedBaselineModel`` → dedicated form model.
    - ``PoissonMatchModel`` → advanced match predictor.
    - ``MatchSimulator`` → full Monte Carlo.
    """

    def __init__(
        self,
        minutes_model: MinutesModel | None = None,
        team_model: TeamStrengthModel | None = None,
        match_model: PoissonMatchModel | None = None,
        simulator: MatchSimulator | None = None,
        scoring_engine: FPLScoringEngine | None = None,
        form_baseline: FixtureAdjustedBaselineModel | None = None,
    ) -> None:
        self._minutes_model = minutes_model or MinutesModel()
        self._team_model = team_model or TeamStrengthModel()
        self._match_model = match_model or PoissonMatchModel()
        self._simulator = simulator or MatchSimulator()
        self._scoring_engine = scoring_engine or FPLScoringEngine()
        self._form_baseline = form_baseline or FixtureAdjustedBaselineModel()

    @property
    def model_name(self) -> str:
        return "player_baseline_pipeline"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        position_code: int,
        player_features: dict[str, float],
        home_team_features: dict[str, float] | None = None,
        away_team_features: dict[str, float] | None = None,
        cutoff_time: datetime | None = None,
    ) -> PlayerBaselineOutput:
        """Predict expected FPL points for a player in a fixture.

        Args:
            player_id: Player ID.
            fixture_id: Fixture ID.
            position_code: FPL position (1=GK, 2=DEF, 3=MID, 4=FWD).
            player_features: Pre-computed player feature vector.
            home_team_features: Home team feature dict (for team strength).
            away_team_features: Away team feature dict.
            cutoff_time: The decision cutoff.

        Returns:
            A ``PlayerBaselineOutput``.
        """
        # 1. Expected minutes from the minutes model.
        minutes_pred = self._minutes_model.predict_batch({player_id: player_features}, None).get(
            player_id, {}
        )
        expected_minutes = minutes_pred.get("expected_minutes", 0.0)
        prob_starting = minutes_pred.get("probability_starting", 0.0)

        # 2. Team/match contribution.
        match_prediction = None
        if home_team_features is not None and away_team_features is not None:
            match_prediction = self._match_model.predict_from_features(
                fixture_id,
                cutoff_time or datetime.min,
                home_team_features,
                away_team_features,
            )

        # 3. Form baseline (fixture-adjusted).
        #    We generate an expected-points estimate from features.
        form_pred = self._form_baseline.predict_batch({player_id: player_features}, None).get(
            player_id, {}
        )
        form_xp = form_pred.get("predicted_expected_points", 0.0)

        # 4. Assemble FPL components.
        components = FPLPointsComponents(
            expected_goals=self._estimate_goals(
                form_xp, expected_minutes, match_prediction, position_code
            ),
            expected_assists=self._estimate_assists(
                form_xp, expected_minutes, match_prediction, position_code
            ),
            expected_clean_sheet=self._estimate_clean_sheet(match_prediction, player_features),
            expected_bonus=0.0,  # No bonus model yet in Phase 4.
            appearance_minutes=expected_minutes,
            expected_goals_conceded=self._estimate_goals_conceded(
                match_prediction, player_features
            ),
        )

        # Goal/assist contribution split: a fraction of form points
        # is allocated to goal contributions. For Phase 4 this is an
        # approximation that will be replaced by the advanced player model.
        g_a_share = 0.25 if position_code in (1, 2) else 0.35
        components.expected_goals = form_xp * g_a_share * 0.4
        components.expected_assists = form_xp * g_a_share * 0.3

        # 5. Convert to FPL points.
        pts = self._scoring_engine.expected_points(components, position_code)
        expected_points = pts["total"]

        # 6. Uncertainty approximation.
        #    The 5th-95th range is approximated as +/- 50% of the expected
        #    points (documented limitation). A true distribution requires
        #    full Monte Carlo (Phase 5).
        lower = expected_points * 0.5
        upper = expected_points * 1.5

        # 7. Data completeness.
        completeness = self._compute_completeness(
            player_features, home_team_features, away_team_features
        )

        return PlayerBaselineOutput(
            player_id=player_id,
            fixture_id=fixture_id,
            cutoff_time=cutoff_time or datetime.min,
            expected_minutes=expected_minutes,
            probability_starting=prob_starting,
            expected_points=expected_points,
            points_lower=lower,
            points_upper=upper,
            components=components,
            data_completeness=completeness,
            method="baseline_pipeline_v1",
        )

    def predict_batch(
        self,
        player_features_batch: dict[int, dict[str, float]],
        position_codes: dict[int, int],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, PlayerBaselineOutput]:
        """Predict for multiple players."""
        results: dict[int, PlayerBaselineOutput] = {}
        ctx = context or {}
        for pid, features in player_features_batch.items():
            pos = position_codes.get(pid, 3)
            results[pid] = self.predict(
                player_id=pid,
                fixture_id=int(features.get("fixture_id", 0)),
                position_code=pos,
                player_features=features,
                home_team_features=ctx.get("home_team_features"),
                away_team_features=ctx.get("away_team_features"),
                cutoff_time=getattr(cutoff, "cutoff_time", None),
            )
        return results

    # ------------------------------------------------------------------
    # Component estimates (Phase 4 approximations)
    # ------------------------------------------------------------------

    def _estimate_goals(
        self,
        form_xp: float,
        minutes: float,
        match_prediction: Any,
        position_code: int,
    ) -> float:
        """Approximate expected goals contribution."""
        base = 0.0
        if position_code == 4:  # FWD
            base = 0.3 + form_xp * 0.02
        elif position_code == 3:  # MID
            base = 0.15 + form_xp * 0.01
        # Adjust if match model gives goal expectations.
        if match_prediction is not None:
            total_goals = (
                match_prediction.expected_home_goals + match_prediction.expected_away_goals
            )
            avg_goals = self._match_model._league_avg_goals
            if avg_goals > 0:
                base *= total_goals / avg_goals / 2
        # Minutes adjustment.
        return base * min(1.0, minutes / 90.0)

    def _estimate_assists(
        self, form_xp: float, minutes: float, match_prediction: Any, position_code: int
    ) -> float:
        """Approximate expected assists contribution."""
        base = self._estimate_goals(form_xp, minutes, match_prediction, position_code) * 0.7
        return base

    def _estimate_clean_sheet(self, match_prediction: Any, features: dict[str, float]) -> float:
        if match_prediction is not None:
            is_home = features.get("is_home", 0.5)
            return (
                match_prediction.home_clean_sheet_probability
                if is_home == 1.0
                else match_prediction.away_clean_sheet_probability
            )
        return 0.0

    def _estimate_goals_conceded(self, match_prediction: Any, features: dict[str, float]) -> float:
        if match_prediction is not None:
            is_home = features.get("is_home", 0.5)
            return (
                match_prediction.expected_away_goals
                if is_home == 1.0
                else match_prediction.expected_home_goals
            )
        return 2.0

    def _compute_completeness(
        self,
        player_features: dict[str, float] | None,
        home_features: dict[str, float] | None,
        away_features: dict[str, float] | None,
    ) -> float:
        """Compute an explainable data-completeness score."""
        score = 0.0
        total = 3.0

        if player_features and len(player_features) >= 5:
            score += 1.0
        elif player_features:
            score += 0.5

        if home_features and len(home_features) >= 3:
            score += 1.0
        elif home_features:
            score += 0.5

        if away_features and len(away_features) >= 3:
            score += 1.0
        elif away_features:
            score += 0.5

        return round(min(1.0, score / total), 4)
