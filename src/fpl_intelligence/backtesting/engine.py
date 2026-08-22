from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.backtesting.cutoff import (
    DecisionCutoff,
    InformationAccessPolicy,
    get_gameweek_decision_cutoff,
)
from fpl_intelligence.backtesting.models import (
    BacktestConfig,
    BacktestGameweekResult,
    BacktestRun,
    PlayerPrediction,
)

logger = logging.getLogger(__name__)


class FeatureRegistry(Protocol):
    """Protocol for a feature registry that can compute features as-of a cutoff."""

    def compute_features(
        self,
        db_session: Session,
        cutoff: DecisionCutoff,
        player_ids: list[int] | None = None,
    ) -> dict[int, dict[str, float]]: ...


class PredictionModel(Protocol):
    """Protocol for a prediction model used in backtesting."""

    @property
    def model_name(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        cutoff: DecisionCutoff,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: DecisionCutoff,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]: ...


@dataclass
class _GameweekContext:
    """Internal context for a single gameweek of the backtest loop."""

    cutoff: DecisionCutoff
    available_fixtures: list[Any]
    available_players: list[Any]
    features: dict[int, dict[str, float]]
    predictions: dict[int, dict[str, Any]]
    actual_outcomes: dict[int, dict[str, Any]]


class BacktestEngine:
    """Backtest engine that simulates historical prediction decisions.

    For each gameweek in the configured range:
    1. Determine the decision cutoff.
    2. Freeze all available information as-of that cutoff.
    3. Compute features using only data available at the cutoff.
    4. Generate predictions using the configured prediction model.
    5. Store predictions with the cutoff timestamp.
    6. Reveal actual outcomes **only for evaluation** (strictly separated).
    7. Calculate evaluation metrics.
    8. Store the gameweek result.

    There is a **clear separation** between PREDICTION_TIME (steps 1-5) and
    OUTCOME_TIME (steps 6-7).  Outcome data is never allowed to flow backward
    into prediction features.
    """

    def __init__(
        self,
        db_session: Session,
        feature_registry: FeatureRegistry,
        prediction_model: PredictionModel,
    ) -> None:
        self._db = db_session
        self._features = feature_registry
        self._model = prediction_model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, config: BacktestConfig) -> BacktestRun:
        """Execute a backtest synchronously and return the BacktestRun instance."""
        run = self._create_run(config)
        try:
            self._execute_gameweeks(run, config)
            run.status = "completed"
            self._db.commit()
        except Exception:
            self._db.rollback()
            run.status = "failed"
            self._db.add(run)
            self._db.commit()
            raise
        return run

    def run_async(self, config: BacktestConfig) -> BacktestRun:
        """Execute a backtest asynchronously."""
        run = self._create_run(config)
        run.status = "running"
        self._db.commit()
        try:
            self._execute_gameweeks(run, config)
            run.status = "completed"
            self._db.commit()
        except Exception:
            self._db.rollback()
            run.status = "failed"
            self._db.add(run)
            self._db.commit()
            raise
        return run

    def get_status(self, run_id: str) -> str:
        """Return the status of a backtest run."""
        run = self._db.scalar(select(BacktestRun).where(BacktestRun.run_id == run_id))
        if run is None:
            raise ValueError(f"BacktestRun {run_id!r} not found.")
        return run.status

    def get_results(self, run_id: str) -> list[BacktestGameweekResult]:
        """Return all gameweek results for a backtest run."""
        stmt = (
            select(BacktestGameweekResult)
            .where(BacktestGameweekResult.run_id == run_id)
            .order_by(
                BacktestGameweekResult.season,
                BacktestGameweekResult.gameweek,
            )
        )
        return list(self._db.scalars(stmt).all())

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _create_run(self, config: BacktestConfig) -> BacktestRun:
        """Create a BacktestRun from a config."""
        self._db.add(config)
        self._db.flush()

        run = BacktestRun(
            run_id=str(uuid.uuid4()),
            config_id=config.id,
            feature_version=config.feature_version,
            model_version=config.model_version,
            status="running",
        )
        self._db.add(run)
        self._db.flush()
        return run

    def _execute_gameweeks(self, run: BacktestRun, config: BacktestConfig) -> None:
        """Execute the backtest loop across all configured gameweeks."""
        from fpl_intelligence.backtesting.evaluation import BacktestEvaluator
        from fpl_intelligence.db.models import PlayerGameweekPerformance

        evaluator = BacktestEvaluator()

        start_gw = config.start_gameweek
        end_gw = config.end_gameweek
        season = config.season
        policy = InformationAccessPolicy(config.information_access_policy)

        for gw_num in range(start_gw, end_gw + 1):
            # 1. Determine the decision cutoff for this gameweek.
            cutoff = get_gameweek_decision_cutoff(self._db, season, gw_num, policy)

            # 2. Compute features using only pre-cutoff data.
            features = self._features.compute_features(self._db, cutoff)

            # 3. Generate predictions for all players with features.
            predictions = self._model.predict_batch(features, cutoff, {"db": self._db})

            # 4. Persist individual player predictions.
            for player_id, pred in predictions.items():
                player_pred = PlayerPrediction(
                    run_id=run.id,
                    player_id=player_id,
                    cutoff=cutoff.cutoff_time,
                    predicted_expected_points=pred.get("predicted_expected_points"),
                    prediction_interval_lower=pred.get("prediction_interval_lower"),
                    prediction_interval_upper=pred.get("prediction_interval_upper"),
                    feature_version=config.feature_version,
                    model_version=config.model_version,
                    confidence=pred.get("confidence"),
                    data_completeness=pred.get("data_completeness"),
                    is_frozen=True,
                )
                self._db.add(player_pred)

            # 5. Reveal actual outcomes (strictly separated for evaluation only).
            actuals: dict[int, dict[str, Any]] = {}
            gw_perfs = list(
                self._db.execute(
                    select(PlayerGameweekPerformance).where(
                        PlayerGameweekPerformance.gameweek_id == gw_num
                    )
                )
                .scalars()
                .all()
            )
            for perf in gw_perfs:
                actuals[perf.player_id] = {
                    "actual_points": perf.total_points or 0,
                    "actual_minutes": perf.minutes or 0,
                }

            # 6. Evaluate.
            metrics = evaluator.evaluate(predictions, actuals)

            # 7. Store the gameweek result.
            result = BacktestGameweekResult(
                run_id=run.id,
                season=season,
                gameweek=gw_num,
                decision_cutoff=cutoff.cutoff_time,
                predictions={str(pid): pred for pid, pred in predictions.items()},
                actual_outcomes={str(pid): act for pid, act in actuals.items()},
                evaluation_metrics=metrics,
            )
            self._db.add(result)

        self._db.flush()
