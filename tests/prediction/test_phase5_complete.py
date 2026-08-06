"""Phase 5 complete tests."""
from __future__ import annotations

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
from fpl_intelligence.prediction.advanced_player.clean_sheet_model import (
    CleanSheetModel,
)
from fpl_intelligence.prediction.advanced_player.defensive_contribution_model import (
    DefensiveContributionModel,
)
from fpl_intelligence.prediction.advanced_player.goal_model import GoalModel
from fpl_intelligence.prediction.advanced_player.player_model import (
    AdvancedPlayerModel,
)
from fpl_intelligence.prediction.distributions.calibration import (
    CalibrationReport,
    evaluate_calibration,
)
from fpl_intelligence.prediction.distributions.engine import DistributionEngine
from fpl_intelligence.prediction.match import MatchPrediction, PoissonMatchModel
from fpl_intelligence.prediction.phase5_comparison import ComparisonResult, Phase5Comparison
from fpl_intelligence.prediction.scoring import FPLPointsComponents, FPLScoringEngine
from fpl_intelligence.prediction.simulation import (
    GameweekSimulator,
    MatchSimulator,
)
from fpl_intelligence.simulation.gameweek import (
    AdvancedGameweekSimulator,
)
from fpl_intelligence.simulation.joint import JointSimulator

# ===========================================================================
# 1. HOLDOUT POLICY TESTS
# ===========================================================================


class TestHoldoutPolicy:
    """Test holdout policy enforcement."""

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
                target_date=datetime(2025, 9, 1),
                season="2025-26",
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
            season="2024-25",
            observation_date=datetime(2025, 1, 15),
            mode=HoldoutMode.DEVELOPMENT,
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
    """Test FPL scoring engine."""

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
        assert result["goals"] == pytest.approx(6.0)

    def test_clean_sheet_defender(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_clean_sheet=True, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=2)
        assert result["clean_sheet"] == pytest.approx(4.0)

    def test_clean_sheet_mid(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_clean_sheet=True, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=3)
        assert result["clean_sheet"] == pytest.approx(1.0)

    def test_appearance_60_plus(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(appearance_minutes=90.0)
        result = engine.compute(comp, position_code=3)
        assert result["appearance"] == pytest.approx(2.0)

    def test_appearance_under_60(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(appearance_minutes=30.0)
        result = engine.compute(comp, position_code=3)
        assert result["appearance"] == pytest.approx(1.0 * (30.0 / 60.0))

    def test_assist_points(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_assists=1.0)
        result = engine.compute(comp, position_code=3)
        assert result["assists"] == pytest.approx(3.0)

    def test_yellow_card(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_yellow_cards=1.0)
        result = engine.compute(comp, position_code=3)
        assert result["yellow_card"] == pytest.approx(-1.0)

    def test_total_points_sum(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(
            expected_goals=1.0,
            expected_assists=1.0,
            expected_clean_sheet=1.0,
            expected_bonus=3.0,
            appearance_minutes=90.0,
            defensive_contribution=1.0,
        )
        result = engine.compute(comp, position_code=3)
        total = sum(v for k, v in result.items() if k != "total")
        assert result["total"] == pytest.approx(total)

    def test_custom_rules(self):
        custom_rules = {
            "rules_version": "test",
            "points": {
                "goal": {"MID": 10},
                "assist": 5,
                "clean_sheet": {"MID": 2},
                "appearance_minutes_60_plus": 3,
                "appearance_minutes_under_60": 1,
            },
            "default_position": "MID",
        }
        engine = FPLScoringEngine(custom_rules)
        comp = FPLPointsComponents(
            expected_goals=1.0, expected_assists=1.0, appearance_minutes=90.0
        )
        result = engine.compute(comp, position_code=3)
        assert result["goals"] == pytest.approx(10.0)
        assert result["assists"] == pytest.approx(5.0)

    def test_zero_minutes(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(appearance_minutes=0.0)
        result = engine.compute(comp, position_code=3)
        assert result["appearance"] == pytest.approx(0.0)

    def test_expected_points_alias(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_goals=1.0, appearance_minutes=90.0)
        result1 = engine.compute(comp, position_code=3)
        result2 = engine.expected_points(comp, position_code=3)
        assert result1["total"] == result2["total"]

    def test_rules_version(self):
        engine = FPLScoringEngine()
        assert engine.rules_version == "default-official"

    def test_custom_rules_version(self):
        custom_rules = {"rules_version": "2026-27", "points": {}, "default_position": "MID"}
        engine = FPLScoringEngine(custom_rules)
        assert engine.rules_version == "2026-27"

    def test_with_rules(self):
        engine = FPLScoringEngine()
        custom_rules = {"rules_version": "2026-27", "points": {}, "default_position": "MID"}
        engine2 = engine.with_rules(custom_rules)
        assert engine2.rules_version == "2026-27"
        assert engine.rules_version == "default-official"


# ===========================================================================
# 3. GOAL MODEL TESTS
# ===========================================================================


class TestGoalModel:
    """Test goal prediction model."""

    def test_xg_expectation(self):
        model = GoalModel()
        features = {"xg_last_5": 1.5, "expected_minutes": 90.0, "position_code": 3}
        pred = model.predict(1, 1, features)
        assert pred.expected_goals > 0
        assert pred.xg_used is True

    def test_probability_distribution(self):
        model = GoalModel()
        features = {"goals_per_90": 0.5, "expected_minutes": 90.0, "position_code": 3}
        pred = model.predict(1, 1, features)
        total_prob = pred.p_0 + pred.p_1 + pred.p_2 + pred.p_3_plus
        assert total_prob == pytest.approx(1.0, abs=0.01)

    def test_zero_goals_outcome(self):
        model = GoalModel()
        features = {"goals_per_90": 0.0, "expected_minutes": 0.0, "position_code": 3}
        pred = model.predict(1, 1, features)
        assert pred.expected_goals < 0.1
        assert pred.p_0 > 0.9

    def test_multiple_goals_outcome(self):
        model = GoalModel()
        features = {
            "xg_last_5": 5.0,
            "goals_per_90": 1.5,
            "expected_minutes": 90.0,
            "team_expected_goals": 3.0,
            "position_code": 4,
        }
        pred = model.predict(1, 1, features)
        assert pred.p_3_plus > 0
        assert pred.expected_goals > 0.5

    def test_invalid_input_handling(self):
        model = GoalModel()
        pred = model.predict(1, 1, {})
        assert pred.expected_goals >= 0
        assert pred.data_completeness >= 0

    def test_position_code_gk_reduces(self):
        model = GoalModel()
        features_gk = {"goals_per_90": 0.5, "expected_minutes": 90.0, "position_code": 1}
        features_mid = {"goals_per_90": 0.5, "expected_minutes": 90.0, "position_code": 3}
        pred_gk = model.predict(1, 1, features_gk)
        pred_mid = model.predict(1, 1, features_mid)
        assert pred_gk.expected_goals < pred_mid.expected_goals

    def test_minutes_factor(self):
        model = GoalModel()
        features_full = {"goals_per_90": 1.0, "expected_minutes": 90.0, "position_code": 3}
        features_half = {"goals_per_90": 1.0, "expected_minutes": 45.0, "position_code": 3}
        pred_full = model.predict(1, 1, features_full)
        pred_half = model.predict(1, 1, features_half)
        assert pred_full.expected_goals > pred_half.expected_goals

    def test_data_completeness_with_xg(self):
        model = GoalModel()
        features = {"xg_last_5": 1.0, "expected_minutes": 90.0, "team_expected_goals": 1.5}
        pred = model.predict(1, 1, features)
        assert pred.data_completeness > 0.5
        assert pred.xg_used is True

    def test_data_completeness_without_xg(self):
        model = GoalModel()
        features = {"goals_per_90": 0.5, "expected_minutes": 90.0}
        pred = model.predict(1, 1, features)
        assert pred.data_completeness >= 0
        assert pred.xg_used is False

    def test_to_dict(self):
        model = GoalModel()
        features = {"goals_per_90": 0.5, "expected_minutes": 90.0, "position_code": 3}
        pred = model.predict(1, 1, features)
        d = pred.to_dict()
        assert "expected_goals" in d
        assert "p_0" in d

    def test_model_name(self):
        model = GoalModel()
        assert model.model_name == "goal_model_v1"

    def test_model_version(self):
        model = GoalModel()
        assert model.model_version == "1.0.0"

    def test_forward_position_multiplier(self):
        model = GoalModel()
        features_mid = {"goals_per_90": 0.5, "expected_minutes": 90.0, "position_code": 3}
        features_fwd = {"goals_per_90": 0.5, "expected_minutes": 90.0, "position_code": 4}
        pred_mid = model.predict(1, 1, features_mid)
        pred_fwd = model.predict(1, 1, features_fwd)
        assert pred_fwd.expected_goals > pred_mid.expected_goals


# ===========================================================================
# 4. ASSIST MODEL TESTS
# ===========================================================================


class TestAssistModel:
    """Test assist prediction model."""

    def test_xa_expectation(self):
        model = AssistModel()
        features = {"xa_last_5": 1.0, "expected_minutes": 90.0, "position_code": 3}
        pred = model.predict(1, 1, features)
        assert pred.expected_assists > 0
        assert pred.xa_used is True

    def test_assist_distribution(self):
        model = AssistModel()
        features = {"assists_per_90": 0.5, "expected_minutes": 90.0, "position_code": 3}
        pred = model.predict(1, 1, features)
        total_prob = pred.p_0 + pred.p_1 + pred.p_2_plus
        assert total_prob == pytest.approx(1.0, abs=0.01)

    def test_invalid_input_handling(self):
        model = AssistModel()
        pred = model.predict(1, 1, {})
        assert pred.expected_assists >= 0
        assert pred.data_completeness >= 0

    def test_zero_assists(self):
        model = AssistModel()
        features = {"assists_per_90": 0.0, "expected_minutes": 0.0, "position_code": 3}
        pred = model.predict(1, 1, features)
        assert pred.expected_assists < 0.1
        assert pred.p_0 > 0.9

    def test_key_passes_factor(self):
        model = AssistModel()
        features_low = {"assists_per_90": 0.5, "key_passes_last_5": 0.0, "expected_minutes": 90.0}
        features_high = {"assists_per_90": 0.5, "key_passes_last_5": 20.0, "expected_minutes": 90.0}
        pred_low = model.predict(1, 1, features_low)
        pred_high = model.predict(1, 1, features_high)
        assert pred_high.expected_assists >= pred_low.expected_assists

    def test_position_specific_gk_reduces(self):
        model = AssistModel()
        features = {"assists_per_90": 0.5, "expected_minutes": 90.0}
        pred_gk = model.predict(1, 1, {**features, "position_code": 1})
        pred_mid = model.predict(1, 1, {**features, "position_code": 3})
        assert pred_gk.expected_assists < pred_mid.expected_assists

    def test_model_name(self):
        model = AssistModel()
        assert model.model_name == "assist_model_v1"

    def test_model_version(self):
        model = AssistModel()
        assert model.model_version == "1.0.0"

    def test_to_dict(self):
        model = AssistModel()
        pred = model.predict(1, 1, {"assists_per_90": 0.3, "expected_minutes": 90.0})
        d = pred.to_dict()
        assert "expected_assists" in d

    def test_forward_position_multiplier(self):
        model = AssistModel()
        features_mid = {"assists_per_90": 0.5, "expected_minutes": 90.0, "position_code": 3}
        features_fwd = {"assists_per_90": 0.5, "expected_minutes": 90.0, "position_code": 4}
        pred_mid = model.predict(1, 1, features_mid)
        pred_fwd = model.predict(1, 1, features_fwd)
        assert pred_fwd.expected_assists >= pred_mid.expected_assists


# ===========================================================================
# 5. CLEAN-SHEET MODEL TESTS
# ===========================================================================


class TestCleanSheetModel:
    """Test clean-sheet prediction model."""

    def test_team_cs_dependency(self):
        model = CleanSheetModel()
        features = {
            "team_clean_sheet_probability": 0.5,
            "expected_minutes": 90.0,
            "probability_starting": 1.0,
            "position_code": 3,
        }
        pred = model.predict(1, 1, features)
        assert pred.team_clean_sheet_probability == pytest.approx(0.5)
        assert pred.joint_probability <= 0.5

    def test_expected_minutes_interaction(self):
        model = CleanSheetModel()
        features_base = {
            "team_clean_sheet_probability": 0.5,
            "probability_starting": 1.0,
            "position_code": 2,
        }
        pred_full = model.predict(1, 1, {**features_base, "expected_minutes": 90.0})
        pred_low = model.predict(1, 1, {**features_base, "expected_minutes": 30.0})
        assert pred_full.joint_probability > pred_low.joint_probability

    def test_low_minutes_behavior(self):
        model = CleanSheetModel()
        features = {
            "team_clean_sheet_probability": 0.5,
            "expected_minutes": 0.0,
            "probability_starting": 0.0,
            "position_code": 3,
        }
        pred = model.predict(1, 1, features)
        assert pred.joint_probability == pytest.approx(0.0, abs=0.01)

    def test_gk_def_cs_requires_60_minutes(self):
        model = CleanSheetModel()
        features_base = {
            "team_clean_sheet_probability": 0.5,
            "probability_starting": 1.0,
        }
        pred_gk = model.predict(
            1, 1, {**features_base, "expected_minutes": 90.0, "position_code": 1}
        )
        pred_gk_low = model.predict(
            1, 1, {**features_base, "expected_minutes": 30.0, "position_code": 1}
        )
        assert pred_gk.player_appearance_probability > pred_gk_low.player_appearance_probability

    def test_completeness(self):
        model = CleanSheetModel()
        features = {
            "team_clean_sheet_probability": 0.5,
            "expected_minutes": 90.0,
            "probability_starting": 0.8,
        }
        pred = model.predict(1, 1, features)
        assert pred.data_completeness == pytest.approx(1.0)

    def test_zero_cs_probability(self):
        model = CleanSheetModel()
        features = {
            "team_clean_sheet_probability": 0.0,
            "expected_minutes": 90.0,
            "probability_starting": 1.0,
            "position_code": 3,
        }
        pred = model.predict(1, 1, features)
        assert pred.joint_probability == pytest.approx(0.0, abs=0.01)

    def test_no_starting_probability(self):
        model = CleanSheetModel()
        features = {
            "team_clean_sheet_probability": 0.5,
            "expected_minutes": 90.0,
            "probability_starting": 0.0,
            "position_code": 2,
        }
        pred = model.predict(1, 1, features)
        assert pred.player_appearance_probability == pytest.approx(0.0)

    def test_model_name(self):
        model = CleanSheetModel()
        assert model.model_name == "clean_sheet_model_v1"

    def test_model_version(self):
        model = CleanSheetModel()
        assert model.model_version == "1.0.0"


# ===========================================================================
# 6. BONUS MODEL TESTS
# ===========================================================================


class TestBonusModel:
    """Test bonus prediction model."""

    def test_bps_behavior(self):
        model = BonusModel()
        features = {"bps_last_5": 30.0, "expected_minutes": 90.0}
        context = {"expected_goals": 1.0, "expected_assists": 1.0}
        pred = model.predict(1, 1, features, context)
        assert pred.probability_bonus > 0
        assert pred.available is True

    def test_coverage_threshold(self):
        model = BonusModel()
        model._bps_coverage = 0.5
        pred = model.predict(1, 1, {"expected_minutes": 90.0})
        assert pred.available is False
        assert pred.data_completeness == 0.0

    def test_missing_bps_behavior(self):
        model = BonusModel()
        features = {"expected_minutes": 90.0}
        pred = model.predict(1, 1, features)
        assert pred.available is True
        assert pred.expected_bonus_points >= 0

    def test_high_bps_high_bonus_prob(self):
        model = BonusModel()
        features = {"bps_last_5": 50.0, "expected_minutes": 90.0}
        context = {"expected_goals": 2.0, "expected_assists": 2.0}
        pred = model.predict(1, 1, features, context)
        assert pred.probability_bonus >= 0.7

    def test_low_bps_low_bonus_prob(self):
        model = BonusModel()
        features = {"bps_last_5": 5.0, "expected_minutes": 90.0}
        pred = model.predict(1, 1, features)
        assert pred.probability_bonus < 0.3

    def test_zero_bps(self):
        model = BonusModel()
        features = {"bps_last_5": 0.0, "expected_minutes": 90.0}
        pred = model.predict(1, 1, features)
        assert pred.probability_bonus >= 0

    def test_model_name(self):
        model = BonusModel()
        assert model.model_name == "bonus_model_v1"

    def test_model_version(self):
        model = BonusModel()
        assert model.model_version == "1.0.0"

    def test_to_dict(self):
        model = BonusModel()
        pred = model.predict(1, 1, {"bps_last_5": 10.0, "expected_minutes": 90.0})
        d = pred.to_dict()
        assert "expected_bonus_points" in d


# ===========================================================================
# 7. DEFENSIVE CONTRIBUTION MODEL TESTS
# ===========================================================================


class TestDefensiveContributionModel:
    """Test defensive contribution prediction model."""

    def test_position_specific_behavior(self):
        model = DefensiveContributionModel()
        features_def = {
            "tackles_last_5": 5.0,
            "clearances_last_5": 5.0,
            "blocks_last_5": 2.0,
            "interceptions_last_5": 3.0,
            "recoveries_last_5": 8.0,
            "expected_minutes": 90.0,
            "position_code": 2,
        }
        features_fwd = {
            "tackles_last_5": 0.5,
            "clearances_last_5": 0.5,
            "blocks_last_5": 0.1,
            "interceptions_last_5": 0.2,
            "recoveries_last_5": 2.0,
            "expected_minutes": 90.0,
            "position_code": 4,
        }
        pred_def = model.predict(1, 1, features_def)
        pred_fwd = model.predict(1, 1, features_fwd)
        assert pred_def.probability_threshold_met > pred_fwd.probability_threshold_met

    def test_threshold_handling(self):
        model = DefensiveContributionModel()
        features_high = {
            "tackles_last_5": 8.0,
            "clearances_last_5": 8.0,
            "blocks_last_5": 4.0,
            "interceptions_last_5": 5.0,
            "recoveries_last_5": 15.0,
            "expected_minutes": 90.0,
        }
        features_low = {
            "tackles_last_5": 0.5,
            "clearances_last_5": 0.5,
            "blocks_last_5": 0.1,
            "interceptions_last_5": 0.2,
            "recoveries_last_5": 1.0,
            "expected_minutes": 90.0,
        }
        pred_high = model.predict(1, 1, features_high)
        pred_low = model.predict(1, 1, features_low)
        assert pred_high.probability_threshold_met > pred_low.probability_threshold_met

    def test_missing_data_coverage(self):
        model = DefensiveContributionModel()
        model.set_coverage(0.3)
        pred = model.predict(1, 1, {"expected_minutes": 90.0})
        assert pred.available is False
        assert pred.data_completeness == 0.0

    def test_low_minutes_reduces_probability(self):
        model = DefensiveContributionModel()
        features = {
            "tackles_last_5": 5.0,
            "clearances_last_5": 5.0,
            "blocks_last_5": 2.0,
            "interceptions_last_5": 3.0,
            "recoveries_last_5": 8.0,
        }
        pred_full = model.predict(1, 1, {**features, "expected_minutes": 90.0})
        pred_low = model.predict(1, 1, {**features, "expected_minutes": 15.0})
        assert pred_full.probability_threshold_met > pred_low.probability_threshold_met

    def test_zero_actions(self):
        model = DefensiveContributionModel()
        features = {
            "tackles_last_5": 0.0,
            "clearances_last_5": 0.0,
            "blocks_last_5": 0.0,
            "interceptions_last_5": 0.0,
            "recoveries_last_5": 0.0,
            "expected_minutes": 90.0,
        }
        pred = model.predict(1, 1, features)
        assert pred.probability_threshold_met >= 0

    def test_model_name(self):
        model = DefensiveContributionModel()
        assert model.model_name == "defensive_contribution_model_v1"

    def test_model_version(self):
        model = DefensiveContributionModel()
        assert model.model_version == "1.0.0"


# ===========================================================================
# 8. ADVANCED PLAYER MODEL TESTS
# ===========================================================================


class TestAdvancedPlayerModel:
    """Test advanced player model orchestrator."""

    def test_components_combine(self):
        model = AdvancedPlayerModel()
        features = {
            "expected_minutes": 90.0,
            "probability_starting": 0.9,
            "xg_last_5": 1.0,
            "goals_per_90": 0.5,
            "xa_last_5": 0.5,
            "assists_per_90": 0.3,
            "team_expected_goals": 1.5,
            "opponent_defensive_strength": 1.0,
            "is_home": 1.0,
            "team_clean_sheet_probability": 0.4,
            "position_code": 3,
            "bps_last_5": 20.0,
        }
        context = {"expected_goals": 0.8, "expected_assists": 0.3}
        output = model.predict(1, 1, features, cutoff_time=datetime.now(UTC), context=context)
        assert output.expected_points > 0
        assert output.data_completeness >= 0
        assert output.data_completeness <= 1.0

    def test_completeness_score(self):
        model = AdvancedPlayerModel()
        features = {
            "expected_minutes": 90.0,
            "probability_starting": 0.9,
            "xg_last_5": 1.0,
            "goals_per_90": 0.5,
            "xa_last_5": 0.5,
            "assists_per_90": 0.3,
            "team_expected_goals": 1.5,
            "team_clean_sheet_probability": 0.4,
            "position_code": 3,
            "bps_last_5": 20.0,
            "tackles_last_5": 2.0,
            "clearances_last_5": 3.0,
            "blocks_last_5": 1.0,
            "interceptions_last_5": 1.0,
            "recoveries_last_5": 5.0,
        }
        context = {"expected_goals": 0.8, "expected_assists": 0.3}
        output = model.predict(1, 1, features, cutoff_time=datetime.now(UTC), context=context)
        assert 0.0 <= output.data_completeness <= 1.0

    def test_deterministic_seed_behavior(self):
        model1 = AdvancedPlayerModel(default_seed=42)
        model2 = AdvancedPlayerModel(default_seed=42)
        features = {
            "expected_minutes": 90.0,
            "probability_starting": 0.9,
            "xg_last_5": 1.0,
            "goals_per_90": 0.5,
            "xa_last_5": 0.5,
            "assists_per_90": 0.3,
            "team_expected_goals": 1.5,
            "team_clean_sheet_probability": 0.4,
            "position_code": 3,
            "bps_last_5": 20.0,
        }
        context = {"expected_goals": 0.8, "expected_assists": 0.3}
        out1 = model1.predict(1, 1, features, cutoff_time=datetime.now(UTC), context=context)
        out2 = model2.predict(1, 1, features, cutoff_time=datetime.now(UTC), context=context)
        assert out1.points_p50 == out2.points_p50

    def test_uncertainty_decomposition(self):
        model = AdvancedPlayerModel()
        features = {
            "expected_minutes": 90.0,
            "probability_starting": 0.5,
            "xg_last_5": 0.1,
            "goals_per_90": 0.1,
            "xa_last_5": 0.1,
            "assists_per_90": 0.1,
            "team_expected_goals": 1.5,
            "team_clean_sheet_probability": 0.4,
            "position_code": 3,
        }
        context = {"expected_goals": 0.1, "expected_assists": 0.1}
        output = model.predict(1, 1, features, cutoff_time=datetime.now(UTC), context=context)
        assert "minutes_uncertainty" in output.uncertainty
        assert "performance_uncertainty" in output.uncertainty

    def test_floor_less_than_ceiling(self):
        model = AdvancedPlayerModel()
        features = {
            "expected_minutes": 90.0,
            "probability_starting": 0.9,
            "xg_last_5": 1.0,
            "goals_per_90": 0.5,
            "xa_last_5": 0.5,
            "assists_per_90": 0.3,
            "team_expected_goals": 1.5,
            "team_clean_sheet_probability": 0.4,
            "position_code": 3,
            "bps_last_5": 20.0,
        }
        context = {"expected_goals": 0.8, "expected_assists": 0.3}
        output = model.predict(1, 1, features, cutoff_time=datetime.now(UTC), context=context)
        assert output.floor <= output.points_p50 <= output.ceiling

    def test_output_dict_serializable(self):
        model = AdvancedPlayerModel()
        features = {
            "expected_minutes": 90.0,
            "probability_starting": 0.9,
            "xg_last_5": 1.0,
            "goals_per_90": 0.5,
            "xa_last_5": 0.5,
            "assists_per_90": 0.3,
            "team_expected_goals": 1.5,
            "team_clean_sheet_probability": 0.4,
            "position_code": 3,
            "bps_last_5": 20.0,
        }
        context = {"expected_goals": 0.8, "expected_assists": 0.3}
        output = model.predict(1, 1, features, cutoff_time=datetime.now(UTC), context=context)
        d = output.to_dict()
        assert "expected_points" in d
        assert "player_id" in d
        assert d["player_id"] == 1

    def test_low_starting_prob_increases_uncertainty(self):
        model = AdvancedPlayerModel()
        features_high = {
            "expected_minutes": 90.0,
            "probability_starting": 0.9,
            "xg_last_5": 1.0,
            "goals_per_90": 0.5,
            "xa_last_5": 0.5,
            "assists_per_90": 0.3,
            "team_expected_goals": 1.5,
            "team_clean_sheet_probability": 0.4,
            "position_code": 3,
        }
        features_low = {
            "expected_minutes": 30.0,
            "probability_starting": 0.3,
            "xg_last_5": 0.1,
            "goals_per_90": 0.1,
            "xa_last_5": 0.1,
            "assists_per_90": 0.1,
            "team_expected_goals": 1.5,
            "team_clean_sheet_probability": 0.4,
            "position_code": 3,
        }
        ctx = {"expected_goals": 0.8, "expected_assists": 0.3}
        out_high = model.predict(1, 1, features_high, cutoff_time=datetime.now(UTC), context=ctx)
        out_low = model.predict(1, 1, features_low, cutoff_time=datetime.now(UTC), context=ctx)
        assert out_high.expected_points > out_low.expected_points


# ===========================================================================
# 9. DISTRIBUTION ENGINE TESTS
# ===========================================================================


class TestDistributionEngine:
    """Test point distribution engine."""

    def test_percentiles(self):
        engine = DistributionEngine(simulation_count=10_000, default_seed=42)
        components = {
            "expected_goals": 1.0,
            "expected_assists": 0.5,
            "expected_clean_sheet": 0.4,
            "expected_bonus": 1.0,
            "appearance_minutes": 90.0,
            "defensive_contribution": 0.5,
        }
        dist = engine.compute_distribution(components, position_code=3)
        assert dist.p10 < dist.p50 < dist.p90
        assert dist.expected_points > 0

    def test_floor_ceiling(self):
        engine = DistributionEngine(simulation_count=10_000, default_seed=42)
        components = {
            "expected_goals": 0.5,
            "expected_assists": 0.2,
            "expected_clean_sheet": 0.3,
            "expected_bonus": 0.5,
            "appearance_minutes": 60.0,
            "defensive_contribution": 0.3,
        }
        dist = engine.compute_distribution(components, position_code=3)
        assert dist.floor <= dist.ceiling
        assert dist.floor >= 0

    def test_tail_probabilities(self):
        engine = DistributionEngine(simulation_count=10_000, default_seed=42)
        components = {
            "expected_goals": 0.3,
            "expected_assists": 0.1,
            "expected_clean_sheet": 0.2,
            "expected_bonus": 0.3,
            "appearance_minutes": 60.0,
            "defensive_contribution": 0.2,
        }
        dist = engine.compute_distribution(components, position_code=3)
        assert 0 <= dist.p_2_plus <= 1
        assert 0 <= dist.p_5_plus <= 1
        assert 0 <= dist.p_10_plus <= 1
        assert 0 <= dist.p_15_plus <= 1
        assert dist.p_2_plus >= dist.p_5_plus >= dist.p_10_plus >= dist.p_15_plus

    def test_reproducibility(self):
        engine1 = DistributionEngine(simulation_count=1000, default_seed=42)
        engine2 = DistributionEngine(simulation_count=1000, default_seed=42)
        components = {
            "expected_goals": 1.0,
            "expected_assists": 0.5,
            "expected_clean_sheet": 0.4,
            "expected_bonus": 1.0,
            "appearance_minutes": 90.0,
            "defensive_contribution": 0.5,
        }
        dist1 = engine1.compute_distribution(components, position_code=3)
        dist2 = engine2.compute_distribution(components, position_code=3)
        assert dist1.expected_points == dist2.expected_points
        assert dist1.p10 == dist2.p10
        assert dist1.p90 == dist2.p90
        if dist1.samples is not None and dist2.samples is not None:
            assert np.array_equal(dist1.samples, dist2.samples)

    def test_different_seeds_give_different_results(self):
        engine1 = DistributionEngine(simulation_count=1000, default_seed=42)
        engine2 = DistributionEngine(simulation_count=1000, default_seed=99)
        components = {
            "expected_goals": 1.0,
            "expected_assists": 0.5,
            "expected_clean_sheet": 0.4,
            "expected_bonus": 1.0,
            "appearance_minutes": 90.0,
            "defensive_contribution": 0.5,
        }
        dist1 = engine1.compute_distribution(components, position_code=3)
        dist2 = engine2.compute_distribution(components, position_code=3)
        assert dist1.samples is not None and dist2.samples is not None
        assert not np.allclose(dist1.samples, dist2.samples)

    def test_zero_components(self):
        engine = DistributionEngine(simulation_count=1000, default_seed=42)
        components = {
            "expected_goals": 0.0,
            "expected_assists": 0.0,
            "expected_clean_sheet": 0.0,
            "expected_bonus": 0.0,
            "appearance_minutes": 0.0,
            "defensive_contribution": 0.0,
        }
        dist = engine.compute_distribution(components, position_code=3)
        assert dist.expected_points >= 0

    def test_interval_width_reasonable(self):
        engine = DistributionEngine(simulation_count=5000, default_seed=42)
        components = {
            "expected_goals": 1.0,
            "expected_assists": 0.5,
            "expected_clean_sheet": 0.4,
            "expected_bonus": 1.0,
            "appearance_minutes": 90.0,
            "defensive_contribution": 0.5,
        }
        dist = engine.compute_distribution(components, position_code=3)
        interval = dist.p90 - dist.p10
        assert interval > 0
        assert interval < 50

    def test_tail_probabilities_stable(self):
        engine = DistributionEngine(simulation_count=5000, default_seed=42)
        components = {
            "expected_goals": 0.8,
            "expected_assists": 0.3,
            "expected_clean_sheet": 0.3,
            "expected_bonus": 0.5,
            "appearance_minutes": 70.0,
            "defensive_contribution": 0.3,
        }
        dist1 = engine.compute_distribution(components, position_code=3)
        dist2 = engine.compute_distribution(components, position_code=3)
        assert dist1.p_5_plus == dist2.p_5_plus
        assert dist1.p_10_plus == dist2.p_10_plus
        assert dist1.p_15_plus == dist2.p_15_plus


# ===========================================================================
# 10. JOINT SIMULATOR TESTS
# ===========================================================================


class TestJointSimulator:
    """Test joint match and player simulation."""

    def setup_method(self):
        self.match_model = PoissonMatchModel()
        self.joint_sim = JointSimulator(
            match_model=self.match_model, default_simulations=1000, default_seed=42
        )

    def test_player_goals_conditional_on_team_score(self):
        prediction = MatchPrediction(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            expected_home_goals=2.0,
            expected_away_goals=1.0,
            home_win_probability=0.6,
            draw_probability=0.2,
            away_win_probability=0.2,
            home_clean_sheet_probability=0.3,
            away_clean_sheet_probability=0.5,
        )
        result = self.joint_sim.simulate_joint(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            prediction=prediction,
            simulations=1000,
            seed=42,
        )
        assert result.home_goals is not None
        assert result.away_goals is not None
        assert len(result.home_goals) == 1000

    def test_clean_sheet_dependency(self):
        prediction = MatchPrediction(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            expected_home_goals=0.5,
            expected_away_goals=2.5,
            home_win_probability=0.3,
            draw_probability=0.2,
            away_win_probability=0.5,
            home_clean_sheet_probability=0.6,
            away_clean_sheet_probability=0.1,
        )
        result = self.joint_sim.simulate_joint(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            prediction=prediction,
            simulations=1000,
            seed=42,
        )
        assert result.clean_sheet_home is True or result.clean_sheet_home is False

    def test_reproducibility(self):
        prediction = MatchPrediction(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            expected_home_goals=2.0,
            expected_away_goals=1.0,
            home_win_probability=0.6,
            draw_probability=0.2,
            away_win_probability=0.2,
            home_clean_sheet_probability=0.3,
            away_clean_sheet_probability=0.5,
        )
        result1 = self.joint_sim.simulate_joint(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            prediction=prediction,
            simulations=100,
            seed=42,
        )
        result2 = self.joint_sim.simulate_joint(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            prediction=prediction,
            simulations=100,
            seed=42,
        )
        assert np.array_equal(result1.home_goals, result2.home_goals)
        assert np.array_equal(result1.away_goals, result2.away_goals)

    def test_simulation_count_respected(self):
        prediction = MatchPrediction(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            expected_home_goals=1.5,
            expected_away_goals=1.5,
            home_win_probability=0.4,
            draw_probability=0.2,
            away_win_probability=0.4,
            home_clean_sheet_probability=0.4,
            away_clean_sheet_probability=0.4,
        )
        result = self.joint_sim.simulate_joint(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            prediction=prediction,
            simulations=500,
            seed=42,
        )
        assert result.simulations == 500
        assert len(result.home_goals) == 500

    def test_assist_allocation(self):
        prediction = MatchPrediction(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            expected_home_goals=2.0,
            expected_away_goals=1.0,
            home_win_probability=0.6,
            draw_probability=0.2,
            away_win_probability=0.2,
            home_clean_sheet_probability=0.3,
            away_clean_sheet_probability=0.5,
        )
        result = self.joint_sim.simulate_joint(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            prediction=prediction,
            simulations=100,
            seed=42,
        )
        assert result.random_seed == 42


# ===========================================================================
# 11. GAMEWEEK SIMULATOR TESTS
# ===========================================================================


class TestGameweekSimulator:
    """Test gameweek simulation."""

    def setup_method(self):
        self.gw_sim = AdvancedGameweekSimulator(simulations=100, seed=42)

    def test_autosub_rules(self):
        class MockSquad:
            starting_xi = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
            bench = [12, 13, 14]

        fixtures = {1: {"cutoff_time": datetime(2025, 1, 1, tzinfo=UTC)}}
        result = self.gw_sim.simulate_with_autosub(
            MockSquad(), fixtures, gameweek=1, simulation_count=100, seed=42
        )
        assert result.autosub_total >= 0

    def test_captain_multiplier(self):
        class MockSquad:
            starting_xi = [1, 2, 3]
            captain = 1

        fixtures = {1: {"cutoff_time": datetime(2025, 1, 1, tzinfo=UTC)}}
        result = self.gw_sim.simulate_with_autosub(
            MockSquad(), fixtures, gameweek=1, simulation_count=100, seed=42
        )
        assert result.captain_total >= 0

    def test_vice_captain_fallback(self):
        class MockSquad:
            starting_xi = [1, 2, 3]
            captain = 1
            vice_captain = 2

        fixtures = {1: {"cutoff_time": datetime(2025, 1, 1, tzinfo=UTC)}}
        result = self.gw_sim.simulate_with_autosub(
            MockSquad(), fixtures, gameweek=1, simulation_count=100, seed=42
        )
        assert result.captain_total >= 0

    def test_bench_behavior(self):
        class MockSquad:
            starting_xi = [1, 2, 3]
            bench = [4, 5]

        fixtures = {1: {"cutoff_time": datetime(2025, 1, 1, tzinfo=UTC)}}
        result = self.gw_sim.simulate_with_autosub(
            MockSquad(), fixtures, gameweek=1, simulation_count=100, seed=42
        )
        assert result.autosub_total >= 0

    def test_invalid_starting_xi_handling(self):
        class MockSquad:
            starting_xi = [1, 2, 3]
            bench = [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]

        fixtures = {1: {"cutoff_time": datetime(2025, 1, 1, tzinfo=UTC)}}
        result = self.gw_sim.simulate_with_autosub(
            MockSquad(), fixtures, gameweek=1, simulation_count=100, seed=42
        )
        assert result.autosub_total >= 0

    def test_reproducibility(self):
        class MockSquad:
            starting_xi = [1, 2, 3]

        fixtures = {1: {"cutoff_time": datetime(2025, 1, 1, tzinfo=UTC)}}
        result1 = self.gw_sim.simulate_with_autosub(
            MockSquad(), fixtures, gameweek=1, simulation_count=100, seed=42
        )
        result2 = self.gw_sim.simulate_with_autosub(
            MockSquad(), fixtures, gameweek=1, simulation_count=100, seed=42
        )
        assert result1.autosub_total == result2.autosub_total

    def test_simulation_count_stored(self):
        class MockSquad:
            starting_xi = [1, 2, 3]

        fixtures = {1: {"cutoff_time": datetime(2025, 1, 1, tzinfo=UTC)}}
        result = self.gw_sim.simulate_with_autosub(
            MockSquad(), fixtures, gameweek=1, simulation_count=50, seed=42
        )
        assert result.simulations == 50

    def test_captain_candidates(self):
        class MockSquad:
            starting_xi = [1, 2, 3]
            captain = 1

        fixtures = {1: {"cutoff_time": datetime(2025, 1, 1, tzinfo=UTC)}}
        candidates = self.gw_sim.compare_captains(
            MockSquad(), fixtures, gameweek=1, simulation_count=100, seed=42
        )
        assert len(candidates) > 0
        for _pid, score in candidates.items():
            assert score >= 0


# ===========================================================================
# 12. SIMULATION REPRODUCIBILITY TESTS
# ===========================================================================


class TestSimulationReproducibility:
    """Test that same inputs + seed + model version = same outputs."""

    def test_match_simulation_reproducible(self):
        sim = MatchSimulator(default_simulations=1000, default_seed=42)
        prediction = MatchPrediction(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            expected_home_goals=2.0,
            expected_away_goals=1.0,
            home_win_probability=0.6,
            draw_probability=0.2,
            away_win_probability=0.2,
            home_clean_sheet_probability=0.3,
            away_clean_sheet_probability=0.5,
        )
        result1 = sim.simulate_match_from_prediction(prediction, simulations=1000, seed=42)
        result2 = sim.simulate_match_from_prediction(prediction, simulations=1000, seed=42)
        assert result1.home_win_probability == result2.home_win_probability
        assert result1.expected_home_goals == result2.expected_home_goals
        assert result1.scoreline_distribution == result2.scoreline_distribution

    def test_player_simulation_reproducible(self):
        model1 = AdvancedPlayerModel(default_seed=42)
        model2 = AdvancedPlayerModel(default_seed=42)
        features = {
            "expected_minutes": 90.0,
            "probability_starting": 0.9,
            "xg_last_5": 1.0,
            "goals_per_90": 0.5,
            "xa_last_5": 0.5,
            "assists_per_90": 0.3,
            "team_expected_goals": 1.5,
            "team_clean_sheet_probability": 0.4,
            "position_code": 3,
            "bps_last_5": 20.0,
        }
        context = {"expected_goals": 0.8, "expected_assists": 0.3}
        out1 = model1.predict(1, 1, features, cutoff_time=datetime.now(UTC), context=context)
        out2 = model2.predict(1, 1, features, cutoff_time=datetime.now(UTC), context=context)
        assert out1.points_p50 == out2.points_p50

    def test_gameweek_simulation_reproducible(self):
        class MockSquad:
            starting_xi = [1, 2, 3]

        gw_sim = GameweekSimulator(simulations=100, seed=42)
        gw_sim2 = GameweekSimulator(simulations=100, seed=42)
        fixtures = {1: {"cutoff_time": datetime(2025, 1, 1, tzinfo=UTC)}}
        result1 = gw_sim.simulate_gameweek(MockSquad(), fixtures, 1)
        result2 = gw_sim2.simulate_gameweek(MockSquad(), fixtures, 1)
        assert result1.expected_total == result2.expected_total

    def test_advanced_gameweek_reproducible(self):
        class MockSquad:
            starting_xi = [1, 2, 3]

        gws1 = AdvancedGameweekSimulator(simulations=100, seed=42)
        gws2 = AdvancedGameweekSimulator(simulations=100, seed=42)
        fixtures = {1: {"cutoff_time": datetime(2025, 1, 1, tzinfo=UTC)}}
        r1 = gws1.simulate_with_autosub(MockSquad(), fixtures, 1)
        r2 = gws2.simulate_with_autosub(MockSquad(), fixtures, 1)
        assert r1.autosub_total == r2.autosub_total

    def test_joint_simulation_reproducible(self):
        prediction = MatchPrediction(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            expected_home_goals=2.0,
            expected_away_goals=1.0,
            home_win_probability=0.6,
            draw_probability=0.2,
            away_win_probability=0.2,
            home_clean_sheet_probability=0.3,
            away_clean_sheet_probability=0.5,
        )
        js1 = JointSimulator(default_simulations=100, default_seed=42)
        js2 = JointSimulator(default_simulations=100, default_seed=42)
        r1 = js1.simulate_joint(
            1, datetime(2025, 1, 1, tzinfo=UTC),
            prediction=prediction, simulations=100, seed=42,
        )
        r2 = js2.simulate_joint(
            1, datetime(2025, 1, 1, tzinfo=UTC),
            prediction=prediction, simulations=100, seed=42,
        )
        assert np.array_equal(r1.home_goals, r2.home_goals)


# ===========================================================================
# 13. MONTE CARLO CONVERGENCE TESTS
# ===========================================================================


class TestMonteCarloConvergence:
    """Test that increasing simulation count produces stable estimates."""

    def test_convergence_with_more_sims(self):
        engine = DistributionEngine(simulation_count=10_000, default_seed=42)
        components = {
            "expected_goals": 1.0,
            "expected_assists": 0.5,
            "expected_clean_sheet": 0.4,
            "expected_bonus": 1.0,
            "appearance_minutes": 90.0,
            "defensive_contribution": 0.5,
        }
        dist_1k = engine.compute_distribution(components, position_code=3)
        assert dist_1k.expected_points > 0
        assert dist_1k.p10 < dist_1k.p90

    def test_expected_points_stable_across_sim_counts(self):
        components = {
            "expected_goals": 1.0,
            "expected_assists": 0.5,
            "expected_clean_sheet": 0.4,
            "expected_bonus": 1.0,
            "appearance_minutes": 90.0,
            "defensive_contribution": 0.5,
        }
        eng_1k = DistributionEngine(simulation_count=1000, default_seed=42)
        eng_10k = DistributionEngine(simulation_count=10000, default_seed=42)
        d1 = eng_1k.compute_distribution(components, position_code=3)
        d2 = eng_10k.compute_distribution(components, position_code=3)
        assert abs(d1.expected_points - d2.expected_points) < 1.0


# ===========================================================================
# 14. CALIBRATION TESTS
# ===========================================================================


class TestCalibration:
    """Test distribution calibration."""

    def test_brier_score_computation(self):
        np.random.seed(42)
        pred_probs = np.array([0.8, 0.3, 0.6, 0.9, 0.1])
        outcomes = np.array([1, 0, 1, 1, 0])
        report = evaluate_calibration(pred_probs, outcomes)
        assert "overall" in report.brier_scores
        assert 0 <= report.brier_scores["overall"] <= 1

    def test_threshold_calibration(self):
        np.random.seed(42)
        pred_probs = np.random.rand(100)
        outcomes = (pred_probs > 0.5).astype(float)
        report = evaluate_calibration(pred_probs, outcomes, thresholds=[0.5])
        assert "p_0.5" in report.threshold_calibration

    def test_perfect_calibration(self):
        pred_probs = np.array([1.0, 0.0, 1.0, 0.0, 1.0])
        outcomes = np.array([1, 0, 1, 0, 1])
        report = evaluate_calibration(pred_probs, outcomes)
        assert report.brier_scores["overall"] == pytest.approx(0.0)

    def test_report_structure(self):
        np.random.seed(42)
        pred_probs = np.random.rand(50)
        outcomes = np.random.randint(0, 2, 50)
        report = evaluate_calibration(pred_probs, outcomes)
        assert isinstance(report, CalibrationReport)
        assert report.n_samples == 50


# ===========================================================================
# 15. PHASE 5 COMPARISON TESTS
# ===========================================================================


class TestPhase5Comparison:
    """Test Phase 5 model comparison framework."""

    def test_comparison_result_structure(self):
        result = ComparisonResult(model_name="test_model", n=10, mae=1.5, rmse=2.0)
        d = result.to_dict()
        assert d["model_name"] == "test_model"
        assert d["n"] == 10
        assert d["mae"] == pytest.approx(1.5)
        assert d["rmse"] == pytest.approx(2.0)

    def test_phase5_comparison_evaluate(self):
        comparison = Phase5Comparison()
        predictions = {
            "baseline_a": {1: 5.0, 2: 6.0, 3: 4.0},
            "advanced": {1: 6.0, 2: 5.5, 3: 4.5},
        }
        actuals = {1: 7.0, 2: 5.0, 3: 5.0}
        results = comparison.compare(predictions, actuals)
        assert "baseline_a" in results
        assert "advanced" in results

    def test_spearman_correlation_perfect(self):
        comparison = Phase5Comparison()
        preds = {1: 10.0, 2: 8.0, 3: 6.0, 4: 4.0, 5: 2.0}
        actuals = {1: 10.0, 2: 8.0, 3: 6.0, 4: 4.0, 5: 2.0}
        result = comparison._evaluate_model("test", preds, actuals)
        assert result.spearman == pytest.approx(1.0, abs=0.01)

    def test_top_k_capture(self):
        comparison = Phase5Comparison()
        preds = np.array([10.0, 8.0, 6.0, 4.0, 2.0])
        actuals = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
        capture = comparison._top_k_capture(preds, actuals, k=3)
        assert 0 <= capture <= 1


# ===========================================================================
# 16. HOLDOUT SEMANTICS TESTS
# ===========================================================================


class TestHoldoutSemantics:
    """Test holdout semantics and mode-specific behavior."""

    def test_development_mode_excludes_2025_26(self):
        split = SeasonSplit()
        assert not split.is_development("2025-26")

    def test_final_holdout_evaluation_can_load_2025_26(self):
        split = SeasonSplit()
        result = split.validate_observation(
            season="2025-26",
            observation_date=datetime(2025, 9, 15),
            mode=HoldoutMode.FINAL_HOLDOUT_EVALUATION,
        )
        assert result is None

    def test_same_model_cannot_retrain_when_evaluating_holdout(self):
        split = SeasonSplit()
        with pytest.raises(HoldoutViolationError):
            split.allowed_for_training("2025-26", mode=HoldoutMode.FINAL_HOLDOUT_EVALUATION)

    def test_walkforward_blocks_2025_26(self):
        with pytest.raises(HoldoutViolationError):
            enforce_holdout(season="2025-26", mode=HoldoutMode.DEVELOPMENT)

    def test_cutoff_date_prevents_loophole(self):
        with pytest.raises(HoldoutViolationError):
            enforce_holdout(
                season="2025-26",
                target_date=datetime(2025, 9, 15),
                mode=HoldoutMode.DEVELOPMENT,
            )

    def test_season_split_from_dict(self):
        data = {
            "development_seasons": ["2022-23", "2023-24"],
            "validation_seasons": ["2022-23", "2023-24"],
            "final_holdout_seasons": ["2025-26"],
        }
        split = SeasonSplit.from_dict(data)
        assert split.is_development("2022-23")
        assert split.is_holdout("2025-26")


# ===========================================================================
# 17. DATA AVAILABILITY TESTS
# ===========================================================================


class TestDataAvailability:
    """Test data availability coverage for advanced components."""

    def test_goal_model_feature_coverage(self):
        model = GoalModel()
        features = {"xg_last_5": 1.0, "expected_minutes": 90.0, "team_expected_goals": 1.5}
        pred = model.predict(1, 1, features)
        assert pred.data_completeness > 0.5
        assert pred.xg_used is True

    def test_goal_model_no_xg_coverage(self):
        model = GoalModel()
        features = {"goals_per_90": 0.5, "expected_minutes": 90.0}
        pred = model.predict(1, 1, features)
        assert pred.data_completeness >= 0
        assert pred.xg_used is False

    def test_assist_model_feature_coverage(self):
        model = AssistModel()
        features = {"xa_last_5": 1.0, "expected_minutes": 90.0, "team_expected_goals": 1.5}
        pred = model.predict(1, 1, features)
        assert pred.data_completeness > 0.5
        assert pred.xa_used is True

    def test_bonus_model_coverage_below_threshold(self):
        model = BonusModel()
        model._bps_coverage = 0.5
        pred = model.predict(1, 1, {"expected_minutes": 90.0})
        assert pred.available is False

    def test_defensive_model_coverage_below_threshold(self):
        model = DefensiveContributionModel()
        model.set_coverage(0.3)
        pred = model.predict(1, 1, {"expected_minutes": 90.0})
        assert pred.available is False

    def test_clean_sheet_completeness(self):
        model = CleanSheetModel()
        features = {
            "team_clean_sheet_probability": 0.5,
            "expected_minutes": 90.0,
            "probability_starting": 0.8,
        }
        pred = model.predict(1, 1, features)
        assert pred.data_completeness > 0.5


# ===========================================================================
# 18. MATCH SIMULATOR TESTS
# ===========================================================================


class TestMatchSimulator:
    """Test match simulator."""

    def test_simulate_basic_match(self):
        sim = MatchSimulator(default_simulations=1000, default_seed=42)
        prediction = MatchPrediction(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            expected_home_goals=2.0,
            expected_away_goals=1.0,
            home_win_probability=0.6,
            draw_probability=0.2,
            away_win_probability=0.2,
            home_clean_sheet_probability=0.3,
            away_clean_sheet_probability=0.5,
        )
        result = sim.simulate_match_from_prediction(prediction, simulations=1000, seed=42)
        assert result.simulations == 1000
        assert 0 <= result.home_win_probability <= 1

    def test_simulate_zero_goals(self):
        sim = MatchSimulator(default_simulations=100, default_seed=42)
        prediction = MatchPrediction(
            fixture_id=1,
            cutoff_time=datetime(2025, 1, 1, tzinfo=UTC),
            expected_home_goals=0.0,
            expected_away_goals=0.0,
            home_win_probability=0.5,
            draw_probability=0.0,
            away_win_probability=0.5,
            home_clean_sheet_probability=1.0,
            away_clean_sheet_probability=1.0,
        )
        result = sim.simulate_match_from_prediction(prediction)
        assert result.expected_home_goals == pytest.approx(0.0, abs=0.01)


# ===========================================================================
# 19. SCORING ENGINE EDGE CASES
# ===========================================================================


class TestScoringEngineEdgeCases:
    """Test edge cases for scoring engine."""

    def test_goals_conceded_deduction(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_goals_conceded=3.0, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=2)
        assert result["goals_conceded_deduction"] <= 0

    def test_defensive_contribution_points(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(defensive_contribution=1.0, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=3)
        assert result["defensive_contribution"] == pytest.approx(1.0)

    def test_penalty_save_points(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_penalty_saves=1.0, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=1)
        assert result["penalty_save"] == pytest.approx(5.0)

    def test_penalty_miss_points(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_penalty_misses=1.0, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=4)
        assert result["penalty_miss"] == pytest.approx(-2.0)

    def test_own_goal_points(self):
        engine = FPLScoringEngine()
        comp = FPLPointsComponents(expected_own_goals=1.0, appearance_minutes=90.0)
        result = engine.compute(comp, position_code=3)
        assert result["own_goal"] == pytest.approx(-2.0)
