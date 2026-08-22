"""Tests for the backtesting engine, baselines, evaluation, and reporting."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fpl_intelligence.backtesting.baselines import (
    FixtureAdjustedBaseline,
    PointsPer90Baseline,
    RecentFormBaseline,
    RollingExpectedPointsBaseline,
)
from fpl_intelligence.backtesting.cutoff import (
    DecisionCutoff,
    get_all_gameweek_cutoffs,
    get_gameweek_decision_cutoff,
)
from fpl_intelligence.backtesting.evaluation import BacktestEvaluator
from fpl_intelligence.backtesting.models import (
    BacktestConfig,
    BacktestRun,
    create_backtest_run_id,
)
from fpl_intelligence.backtesting.policies import AvailabilityPolicy
from fpl_intelligence.backtesting.reporting import BacktestReport
from fpl_intelligence.backtesting.reproducibility import BacktestReproducer
from fpl_intelligence.backtesting.walk_forward import WalkForwardValidator
from fpl_intelligence.db.models import (
    FPLSnapshot,
)
from fpl_intelligence.features.calculators.player_form import PlayerFormCalculator
from fpl_intelligence.features.registry import FeatureRegistry
from fpl_intelligence.features.temporal import InformationAccessPolicy


class TestDecisionCutoff:
    """Tests for the DecisionCutoff model and functions."""

    def test_cutoff_requires_timezone(self) -> None:
        """Test that cutoff_time must be timezone-aware."""
        with pytest.raises(ValueError, match="timezone-aware"):
            DecisionCutoff(
                cutoff_time=datetime(2025, 8, 15, 12, 0, 0),  # No tz
                gameweek=1,
                season="2025-26",
            )

    def test_cutoff_with_timezone(self) -> None:
        """Test that a timezone-aware cutoff is accepted."""
        cutoff = DecisionCutoff(
            cutoff_time=datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC),
            gameweek=1,
            season="2025-26",
        )
        assert cutoff.cutoff_time == datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        assert cutoff.gameweek == 1
        assert cutoff.season == "2025-26"

    def test_get_gameweek_cutoff(self, db_session, populated_db) -> None:
        """Test getting a gameweek decision cutoff."""
        cutoff = get_gameweek_decision_cutoff(db_session, "2025-26", 1)
        assert cutoff.gameweek == 1
        assert cutoff.season == "2025-26"
        assert cutoff.cutoff_time < cutoff.deadline_time

    def test_get_gameweek_cutoff_not_found(self, db_session) -> None:
        """Test that a non-existent gameweek raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            get_gameweek_decision_cutoff(db_session, "2025-26", 99)

    def test_get_all_gameweek_cutoffs(self, db_session, populated_db) -> None:
        """Test getting all gameweek cutoffs."""
        cutoffs = get_all_gameweek_cutoffs(db_session, "2025-26")
        assert len(cutoffs) == 3  # 3 gameweeks in test data
        assert cutoffs[0].gameweek == 1
        assert cutoffs[2].gameweek == 3

    def test_cutoff_is_strict(self) -> None:
        """Test the is_strict property."""
        cutoff = DecisionCutoff(
            cutoff_time=datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC),
            gameweek=1,
            season="2025-26",
            policy=InformationAccessPolicy.STRICT_REPRODUCIBILITY,
        )
        assert cutoff.is_strict is True

    def test_cutoff_not_strict(self) -> None:
        """Test that non-strict policy is not strict."""
        cutoff = DecisionCutoff(
            cutoff_time=datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC),
            gameweek=1,
            season="2025-26",
            policy=InformationAccessPolicy.PUBLIC_AVAILABILITY,
        )
        assert cutoff.is_strict is False


class TestBacktestModels:
    """Tests for backtest data models."""

    def test_create_run_id(self) -> None:
        """Test that run IDs are unique."""
        id1 = create_backtest_run_id()
        id2 = create_backtest_run_id()
        assert id1 != id2
        assert len(id1) == 36  # UUID format

    def test_backtest_config_creation(self, db_session) -> None:
        """Test creating a BacktestConfig."""
        config = BacktestConfig(
            season="2025-26",
            start_gameweek=1,
            end_gameweek=3,
            random_seed=42,
        )
        db_session.add(config)
        db_session.commit()
        assert config.id is not None

    def test_backtest_run_creation(self, db_session) -> None:
        """Test creating a BacktestRun."""
        config = BacktestConfig(
            season="2025-26",
            start_gameweek=1,
            end_gameweek=3,
        )
        db_session.add(config)
        db_session.flush()

        run = BacktestRun(
            run_id=create_backtest_run_id(),
            config_id=config.id,
        )
        db_session.add(run)
        db_session.commit()
        assert run.id is not None
        assert run.status == "running"


class TestBacktestEvaluator:
    """Tests for the BacktestEvaluator."""

    def test_evaluate_perfect_predictions(self) -> None:
        """Test evaluation with perfect predictions."""
        evaluator = BacktestEvaluator()
        predictions = {
            1: {"predicted_expected_points": 10.0},
            2: {"predicted_expected_points": 5.0},
            3: {"predicted_expected_points": 2.0},
        }
        actuals = {
            1: {"actual_points": 10.0},
            2: {"actual_points": 5.0},
            3: {"actual_points": 2.0},
        }
        metrics = evaluator.evaluate(predictions, actuals)
        assert metrics["mae"] == 0.0
        assert metrics["rmse"] == 0.0
        assert metrics["spearman"] == 1.0

    def test_evaluate_poor_predictions(self) -> None:
        """Test evaluation with poor predictions."""
        evaluator = BacktestEvaluator()
        predictions = {
            1: {"predicted_expected_points": 1.0},
            2: {"predicted_expected_points": 2.0},
            3: {"predicted_expected_points": 3.0},
        }
        actuals = {
            1: {"actual_points": 10.0},
            2: {"actual_points": 5.0},
            3: {"actual_points": 2.0},
        }
        metrics = evaluator.evaluate(predictions, actuals)
        assert metrics["mae"] > 0
        assert metrics["rmse"] > 0

    def test_evaluate_no_overlap(self) -> None:
        """Test evaluation with no overlapping players."""
        evaluator = BacktestEvaluator()
        predictions = {1: {"predicted_expected_points": 10.0}}
        actuals = {2: {"actual_points": 5.0}}
        metrics = evaluator.evaluate(predictions, actuals)
        assert metrics["n_common"] == 0
        assert metrics["coverage"] == 0.0

    def test_evaluate_top_k_hit_rates(self) -> None:
        """Test top-k hit rate computation."""
        evaluator = BacktestEvaluator()
        predictions = {
            1: {"predicted_expected_points": 10.0},
            2: {"predicted_expected_points": 8.0},
            3: {"predicted_expected_points": 6.0},
            4: {"predicted_expected_points": 4.0},
            5: {"predicted_expected_points": 2.0},
        }
        actuals = {
            1: {"actual_points": 10.0},
            2: {"actual_points": 8.0},
            3: {"actual_points": 6.0},
            4: {"actual_points": 4.0},
            5: {"actual_points": 2.0},
        }
        metrics = evaluator.evaluate(predictions, actuals)
        assert metrics["top1_hit_rate"] == 1.0
        assert metrics["top3_hit_rate"] == 1.0

    def test_evaluate_by_position(self) -> None:
        """Test evaluation by player position."""
        evaluator = BacktestEvaluator()
        predictions = {
            1: {"predicted_expected_points": 10.0},
            2: {"predicted_expected_points": 5.0},
        }
        actuals = {
            1: {"actual_points": 10.0},
            2: {"actual_points": 5.0},
        }
        positions = {1: 1, 2: 2}  # 1=GK, 2=DEF
        results = evaluator.evaluate_by_position(predictions, actuals, positions)
        assert "GK" in results
        assert "DEF" in results


class TestBaselines:
    """Tests for baseline prediction models."""

    def test_recent_form_baseline(self, db_session, populated_db) -> None:
        """Test the RecentFormBaseline model."""
        from fpl_intelligence.backtesting.cutoff import DecisionCutoff

        model = RecentFormBaseline()
        cutoff = DecisionCutoff(
            cutoff_time=datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC),
            gameweek=1,
            season="2025-26",
        )
        result = model.predict(1, 1, {}, cutoff, {"db": db_session})
        assert "predicted_expected_points" in result
        assert "confidence" in result
        assert "data_completeness" in result
        assert "method" in result

    def test_points_per_90_baseline(self, db_session, populated_db) -> None:
        """Test the PointsPer90Baseline model."""
        from fpl_intelligence.backtesting.cutoff import DecisionCutoff

        model = PointsPer90Baseline()
        cutoff = DecisionCutoff(
            cutoff_time=datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC),
            gameweek=1,
            season="2025-26",
        )
        result = model.predict(1, 1, {}, cutoff, {"db": db_session})
        assert "predicted_expected_points" in result

    def test_rolling_expected_points_baseline(self, db_session, populated_db) -> None:
        """Test the RollingExpectedPointsBaseline model."""
        from fpl_intelligence.backtesting.cutoff import DecisionCutoff

        model = RollingExpectedPointsBaseline()
        cutoff = DecisionCutoff(
            cutoff_time=datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC),
            gameweek=1,
            season="2025-26",
        )
        result = model.predict(1, 1, {}, cutoff, {"db": db_session})
        assert "predicted_expected_points" in result

    def test_fixture_adjusted_baseline(self, db_session, populated_db) -> None:
        """Test the FixtureAdjustedBaseline model."""
        from fpl_intelligence.backtesting.cutoff import DecisionCutoff

        model = FixtureAdjustedBaseline()
        cutoff = DecisionCutoff(
            cutoff_time=datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC),
            gameweek=1,
            season="2025-26",
        )
        result = model.predict(1, 1, {}, cutoff, {"db": db_session})
        assert "predicted_expected_points" in result

    def test_baseline_batch_prediction(self, db_session, populated_db) -> None:
        """Test batch prediction with a baseline model."""
        from fpl_intelligence.backtesting.cutoff import DecisionCutoff

        model = RecentFormBaseline()
        cutoff = DecisionCutoff(
            cutoff_time=datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC),
            gameweek=1,
            season="2025-26",
        )
        features_batch = {1: {}, 2: {}, 3: {}}
        results = model.predict_batch(features_batch, cutoff, {"db": db_session})
        assert len(results) == 3
        assert 1 in results
        assert 2 in results
        assert 3 in results


class TestAvailabilityPolicy:
    """Tests for the AvailabilityPolicy class."""

    def test_is_available_with_temporal_fields(self, db_session, populated_db) -> None:
        """Test is_available with a record that has temporal fields."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        policy = AvailabilityPolicy()

        snapshot = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
        )
        assert policy.is_available(snapshot, cutoff)

    def test_filter_available(self, db_session, populated_db) -> None:
        """Test filtering available entities."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        policy = AvailabilityPolicy()

        available = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
        )
        future = FPLSnapshot(
            player_id=2,
            season_id=1,
            event_time=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
        )

        filtered = policy.filter_available([available, future], cutoff)
        assert len(filtered) == 1
        assert filtered[0].player_id == 1


class TestBacktestReproducer:
    """Tests for the BacktestReproducer."""

    def test_compute_fingerprint(self, db_session) -> None:
        """Test computing a backtest fingerprint."""
        config = BacktestConfig(
            season="2025-26",
            start_gameweek=1,
            end_gameweek=3,
            random_seed=42,
        )
        reproducer = BacktestReproducer(db_session)
        fp = reproducer.compute_fingerprint(config, {"player_form": "1.0.0"}, "baseline")
        assert len(fp) == 64  # SHA-256 hex digest

    def test_fingerprint_deterministic(self, db_session) -> None:
        """Test that fingerprints are deterministic."""
        config = BacktestConfig(
            season="2025-26",
            start_gameweek=1,
            end_gameweek=3,
            random_seed=42,
        )
        reproducer = BacktestReproducer(db_session)
        fp1 = reproducer.compute_fingerprint(config, {"player_form": "1.0.0"}, "baseline")
        fp2 = reproducer.compute_fingerprint(config, {"player_form": "1.0.0"}, "baseline")
        assert fp1 == fp2

    def test_fingerprint_different_config(self, db_session) -> None:
        """Test that different configs produce different fingerprints."""
        config1 = BacktestConfig(
            season="2025-26",
            start_gameweek=1,
            end_gameweek=3,
            random_seed=42,
        )
        config2 = BacktestConfig(
            season="2025-26",
            start_gameweek=1,
            end_gameweek=5,
            random_seed=42,
        )
        reproducer = BacktestReproducer(db_session)
        fp1 = reproducer.compute_fingerprint(config1, {"player_form": "1.0.0"}, "baseline")
        fp2 = reproducer.compute_fingerprint(config2, {"player_form": "1.0.0"}, "baseline")
        assert fp1 != fp2

    def test_reproduce_backtest_not_found(self, db_session) -> None:
        """Test that reproducing a non-existent run raises ValueError."""
        reproducer = BacktestReproducer(db_session)
        with pytest.raises(ValueError, match="not found"):
            reproducer.reproduce_backtest("nonexistent-id")


class TestBacktestReport:
    """Tests for the BacktestReport."""

    def test_generate_report_not_found(self, db_session) -> None:
        """Test that generating a report for a non-existent run raises ValueError."""
        report = BacktestReport(db_session)
        with pytest.raises(ValueError, match="not found"):
            report.generate_report("nonexistent-id")


class TestWalkForwardValidator:
    """Tests for the WalkForwardValidator."""

    def test_validate_not_enough_gameweeks(self, db_session, populated_db) -> None:
        """Test that validation fails with too few gameweeks."""

        registry = FeatureRegistry(db_session)
        registry.register(PlayerFormCalculator())
        model = RecentFormBaseline()

        validator = WalkForwardValidator(db_session, registry, model)

        with pytest.raises(ValueError, match="Not enough gameweeks"):
            validator.validate("2024-25", 1, 3, min_train_gameweeks=3)
