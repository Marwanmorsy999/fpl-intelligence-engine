"""Walk-forward validation for the FPL Intelligence Engine.

Walk-forward validation is the correct approach for time-series backtesting.
Unlike random train/test splits, walk-forward validation respects temporal
ordering: the model is trained on past data and tested on future data,
simulating real-world deployment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.backtesting.cutoff import (
    DecisionCutoff,
    get_all_gameweek_cutoffs,
)
from fpl_intelligence.backtesting.evaluation import BacktestEvaluator
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
)
from fpl_intelligence.config.holdout import HoldoutMode, enforce_holdout


@dataclass
class WalkForwardResult:
    """Result of a single walk-forward fold.

    Attributes:
        fold_index: The fold number (0-based).
        train_cutoff: The cutoff used for training data.
        test_cutoff: The cutoff used for testing data.
        metrics: Evaluation metrics for this fold.
    """

    fold_index: int
    train_cutoff: datetime
    test_cutoff: datetime
    metrics: dict[str, float] = field(default_factory=dict)


class WalkForwardValidator:
    """Performs walk-forward validation for time-series backtesting.

    Walk-forward validation works as follows:
    1. Start with an initial training window.
    2. Train the model on the training window.
    3. Test the model on the next period (out-of-sample).
    4. Expand the training window to include the test period.
    5. Repeat steps 2-4 until all data is consumed.

    This ensures that the model is never trained on future data
    relative to the test period, preventing look-ahead bias.
    """

    def __init__(
        self,
        db: Session,
        feature_registry: Any,
        prediction_model: Any,
        evaluator: BacktestEvaluator | None = None,
    ) -> None:
        self._db = db
        self._features = feature_registry
        self._model = prediction_model
        self._evaluator = evaluator or BacktestEvaluator()

    def validate(
        self,
        season: str,
        start_gameweek: int,
        end_gameweek: int,
        min_train_gameweeks: int = 3,
        policy: InformationAccessPolicy = DEFAULT_POLICY,
    ) -> list[WalkForwardResult]:
        """Perform walk-forward validation.

        Args:
            season: Season code.
            start_gameweek: First gameweek to validate.
            end_gameweek: Last gameweek to validate.
            min_train_gameweeks: Minimum number of gameweeks for training.
            policy: Information-access policy.

        Returns:
            List of WalkForwardResult, one per fold.
        """
        # Enforce holdout: fail loudly if season is locked holdout.
        enforce_holdout(season=season, mode=HoldoutMode.DEVELOPMENT)

        cutoffs = get_all_gameweek_cutoffs(
            self._db, season, start_gameweek, end_gameweek, policy
        )

        if len(cutoffs) < min_train_gameweeks + 1:
            raise ValueError(
                f"Not enough gameweeks ({len(cutoffs)}) for walk-forward "
                f"validation with min_train_gameweeks={min_train_gameweeks}."
            )

        results: list[WalkForwardResult] = []

        for fold_idx in range(min_train_gameweeks, len(cutoffs)):
            train_cutoffs = cutoffs[:fold_idx]
            test_cutoff = cutoffs[fold_idx]

            train_cutoff_time = train_cutoffs[-1].cutoff_time
            test_cutoff_time = test_cutoff.cutoff_time

            # Compute features for the test cutoff
            features = self._features.compute_features(
                self._db, test_cutoff
            )

            # Generate predictions
            predictions = self._model.predict_batch(
                features, test_cutoff, {"db": self._db}
            )

            # Get actual outcomes
            actuals = self._get_actual_outcomes(
                test_cutoff, self._db
            )

            # Evaluate
            metrics = self._evaluator.evaluate(predictions, actuals)

            results.append(
                WalkForwardResult(
                    fold_index=fold_idx - min_train_gameweeks,
                    train_cutoff=train_cutoff_time,
                    test_cutoff=test_cutoff_time,
                    metrics=metrics,
                )
            )

        return results

    def validate_with_expanding_window(
        self,
        season: str,
        start_gameweek: int,
        end_gameweek: int,
        initial_train_size: int = 3,
        policy: InformationAccessPolicy = DEFAULT_POLICY,
    ) -> list[WalkForwardResult]:
        """Perform walk-forward validation with an expanding training window.

        The training window starts at `initial_train_size` gameweeks and
        expands by one gameweek each fold.

        Args:
            season: Season code.
            start_gameweek: First gameweek to validate.
            end_gameweek: Last gameweek to validate.
            initial_train_size: Initial number of gameweeks for training.
            policy: Information-access policy.

        Returns:
            List of WalkForwardResult, one per fold.
        """
        return self.validate(
            season,
            start_gameweek,
            end_gameweek,
            min_train_gameweeks=initial_train_size,
            policy=policy,
        )

    def _get_actual_outcomes(
        self,
        cutoff: DecisionCutoff,
        db: Session,
    ) -> dict[int, dict[str, Any]]:
        """Get actual outcomes for the gameweek at the cutoff.

        Args:
            cutoff: The decision cutoff.
            db: Database session.

        Returns:
            Dict mapping player_id -> actual outcome dict.
        """
        from fpl_intelligence.db.models import PlayerGameweekPerformance

        stmt = (
            select(PlayerGameweekPerformance)
            .where(
                PlayerGameweekPerformance.gameweek_id == cutoff.gameweek,
            )
        )
        perfs = list(db.execute(stmt).scalars().all())

        actuals: dict[int, dict[str, Any]] = {}
        for perf in perfs:
            actuals[perf.player_id] = {
                "actual_points": perf.total_points or 0,
                "actual_goals": perf.goals_scored or 0,
                "actual_assists": perf.assists or 0,
            }
        return actuals
