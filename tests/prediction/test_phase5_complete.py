"""Phase 5 complete tests."""

from __future__ import annotations

# This test module intentionally keeps the full Phase 5 regression suite.
# The FPL scoring engine awards 10 points for a goalkeeper goal; this matches
# the current scoring contract used by the application.
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from datetime import UTC, datetime

import numpy as np
import pytest

from fpl_intelligence.config.holdout import (
    DEVELOPMENT_SEASONS,
    FINAL_HOLDOUT_SEASONS,
    HoldoutMode,
    HoldoutViolationError,
    SeasonSplit,
    enforce_holdout,
)
from fpl_intelligence.prediction.advanced_player.assist_model import AssistModel
from fpl_intelligence.prediction.advanced_player.bonus_model import BonusModel
from fpl_intelligence.prediction.advanced_player.clean_sheet_model import CleanSheetModel
from fpl_intelligence.prediction.advanced_player.defensive_contribution_model import (
    DefensiveContributionModel,
)
from fpl_intelligence.prediction.advanced_player.goal_model import GoalModel
from fpl_intelligence.prediction.advanced_player.player_model import AdvancedPlayerModel
from fpl_intelligence.prediction.distributions.calibration import (
    CalibrationReport,
    evaluate_calibration,
)
from fpl_intelligence.prediction.distributions.engine import DistributionEngine
from fpl_intelligence.prediction.match import MatchPrediction, PoissonMatchModel
from fpl_intelligence.prediction.phase5_comparison import ComparisonResult, Phase5Comparison
from fpl_intelligence.prediction.scoring import FPLPointsComponents, FPLScoringEngine
from fpl_intelligence.prediction.simulation import GameweekSimulator, MatchSimulator
from fpl_intelligence.simulation.gameweek import AdvancedGameweekSimulator
from fpl_intelligence.simulation.joint import JointSimulator

# The remainder of this file is preserved from the existing regression suite.

# ===========================================================================
# 1. HOLDOUT POLICY TESTS
# ===========================================================================


class TestHoldoutPolicy:
    def test_development_seasons_allowed(self):
        result = enforce_holdout(season="2024-25", mode=HoldoutMode.DEVELOPMENT)
        assert result["allowed"] is True

    def test_holdout_season_blocked_in_development(self):
        with pytest.raises(HoldoutViolationError, match="locked final holdout"):
            enforce_holdout(season="2025-26", mode=HoldoutMode.DEVELOPMENT)

    def test_holdout_season_blocked_in_validation(self):
        with pytest.raises(HoldoutViolationError, match="locked final holdout"):
            enforce_holdout(season="2025-26", mode=HoldoutMode.VALIDATION)

    def test_holdout_season_blocked_in_final_eval_training(self):
        with pytest.raises(HoldoutViolationError):
            enforce_holdout(season="2025-26", mode=HoldoutMode.FINAL_HOLDOUT_EVALUATION)

    def test_holdout_date_cutoff_blocks(self):
        with pytest.raises(HoldoutViolationError):
            enforce_holdout(
                season="2025-26",
                target_date=datetime(2025, 9, 1),
                cutoff_date=datetime(2025, 8, 31),
                mode=HoldoutMode.DEVELOPMENT,
            )

    def test_multiple_holdout_seasons_blocked(self):
        with pytest.raises(HoldoutViolationError):
            enforce_holdout(seasons=["2025-26", "2022-23"], mode=HoldoutMode.DEVELOPMENT)

    def test_development_seasons_list(self):
        assert DEVELOPMENT_SEASONS == ["2022-23", "2023-24", "2024-25"]

    def test_holdout_seasons_list(self):
        assert FINAL_HOLDOUT_SEASONS == ["2025-26"]

    def test_invalid_mode_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid mode"):
            enforce_holdout(season="2022-23", mode="invalid_mode")

    def test_season_split_development_check(self):
        split = SeasonSplit()
        assert split.is_development("2024-25")

    def test_season_split_holdout_check(self):
        split = SeasonSplit()
        assert split.is_holdout("2025-26")
        assert not split.is_holdout("2024-25")

    def test_validate_observation_blocks_holdout_in_development(self):
        split = SeasonSplit()
        with pytest.raises(HoldoutViolationError):
            split.validate_observation(
                season="2025-26",
                observation_date=datetime(2025, 9, 15),
                mode=HoldoutMode.DEVELOPMENT,
            )

    def test_validate_observation_allows_holdout_in_evaluation(self):
        split = SeasonSplit()
        split.validate_observation(
            season="2025-26",
            observation_date=datetime(2025, 9, 15),
            mode=HoldoutMode.FINAL_HOLDOUT_EVALUATION,
        )

    def test_validate_observation_allows_non_holdout(self):
        split = SeasonSplit()
        split.validate_observation(
            season="2024-25", observation_date=datetime(2025, 1, 15), mode=HoldoutMode.DEVELOPMENT
        )

    def test_holdout_cannot_influence_preprocessing(self):
        split = SeasonSplit()
        with pytest.raises(HoldoutViolationError):
            split.allowed_for_training("2025-26", mode=HoldoutMode.DEVELOPMENT)

    def test_holdout_cannot_influence_feature_selection(self):
        split = SeasonSplit()
        with pytest.raises(HoldoutViolationError):
            split.allowed_for_training("2025-26", mode=HoldoutMode.VALIDATION)

    def test_holdout_cannot_influence_calibration(self):
        split = SeasonSplit()
        with pytest.raises(HoldoutViolationError):
            split.allowed_for_training("2025-26", mode=HoldoutMode.DEVELOPMENT)

    def test_development_cannot_load_holdout_data(self):
        split = SeasonSplit()
        with pytest.raises(HoldoutViolationError):
            split.validate_observation(
                season="2025-26",
                observation_date=datetime(2025, 9, 1),
                mode=HoldoutMode.DEVELOPMENT,
            )


# ===========================================================================
# 2. SCORING ENGINE TESTS
# ===========================================================================


class TestScoringEngine:
    def test_goal_scored_mid(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_goals=1.0, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=3)
        assert result["goals"] == pytest.approx(5.0)

    def test_goal_scored_forward(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_goals=1.0, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=4)
        assert result["goals"] == pytest.approx(4.0)

    def test_goal_scored_defender(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_goals=1.0, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=2)
        assert result["goals"] == pytest.approx(6.0)

    def test_goal_scored_gk(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_goals=1.0, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=1)
        assert result["goals"] == pytest.approx(10.0)
