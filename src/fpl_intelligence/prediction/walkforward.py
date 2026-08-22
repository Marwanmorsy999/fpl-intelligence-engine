"""Walk-forward training and evaluation for Phase 4 models.

Integrates model training with the existing backtest framework.
For each historical Gameweek:

1. Determine cutoff.
2. Generate eligible training data (features before cutoff, targets after).
3. Train or update model using only prior information.
4. Generate prediction for the next Gameweek.
5. Freeze and persist prediction.
6. Reveal future outcome.
7. Evaluate.

This ensures temporal correctness: no single global model is trained
on all historical data and then evaluated on past Gameweeks retrospectively.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from fpl_intelligence.backtesting.cutoff import (
    DecisionCutoff,
    get_all_gameweek_cutoffs,
)
from fpl_intelligence.config.holdout import HoldoutMode, enforce_holdout
from fpl_intelligence.features.temporal import DEFAULT_POLICY, InformationAccessPolicy
from fpl_intelligence.prediction.base import PredictionModel
from fpl_intelligence.prediction.evaluation import (
    ModelComparisonReport,
)
from fpl_intelligence.prediction.persistence import PredictionPersistence
from fpl_intelligence.prediction.training import TrainingDataBuilder


class WalkForwardTrainer:
    """Walk-forward training pipeline for prediction models.

    For each Gameweek > initial_train_gws:

    1. Compute the decision cutoff.
    2. Build a training dataset using data strictly before the cutoff,
       with targets from the Gameweek immediately after the cutoff.
    3. Train (or re-fit) the model.
    4. Register the model in the registry.
    5. Generate predictions for the *upcoming* Gameweek.
    6. Persist predictions.
    7. Reveal outcomes from the Gameweek after the prediction.
    8. Accumulate results for final evaluation.
    """

    def __init__(
        self,
        db: Session,
        model: PredictionModel,
        feature_version: str = "1.0.0",
        policy: InformationAccessPolicy = DEFAULT_POLICY,
        initial_train_gws: int = 3,
        prediction_persistence: PredictionPersistence | None = None,
    ) -> None:
        self._db = db
        self._model = model
        self._feature_version = feature_version
        self._policy = policy
        self._initial_train_gws = initial_train_gws
        self._builder = TrainingDataBuilder(db, policy)
        self._persistence = prediction_persistence or PredictionPersistence(db)
        self._fold_results: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(
        self,
        season: str,
        start_gameweek: int,
        end_gameweek: int,
        target: str = "minutes",
        entity_type: str = "player",
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Execute walk-forward training and prediction.

        Args:
            season: Season code.
            start_gameweek: First Gameweek to predict.
            end_gameweek: Last Gameweek to predict.
            target: Training target (minutes/started/points/etc.).
            entity_type: ``player`` or ``team``.
            window_start: Optional training window start.
            window_end: Optional training window end.

        Returns:
            List of per-fold result dicts.
        """
        # Enforce holdout: fail loudly if season is locked holdout.
        enforce_holdout(season=season, mode=HoldoutMode.DEVELOPMENT)

        cutoffs = get_all_gameweek_cutoffs(
            self._db, season, start_gameweek, end_gameweek, self._policy
        )
        if len(cutoffs) < self._initial_train_gws + 1:
            raise ValueError(
                f"Need at least {self._initial_train_gws + 1} Gameweeks for "
                f"walk-forward; got {len(cutoffs)}."
            )

        # We'll predict for each cutoff, using data from *earlier* cutoffs to train.
        # For cutoffs < initial_train_gws we skip prediction (not enough training data).

        all_predictions: dict[int, dict[str, Any]] = {}
        all_actuals: dict[int, dict[str, Any]] = {}

        for idx, cutoff in enumerate(cutoffs):
            if idx < self._initial_train_gws:
                # Not enough training data yet; skip prediction but track.
                continue

            # Determine the training cutoff: the cutoff just before this one.
            train_cutoff_time = self._get_train_cutoff(cutoffs, idx)

            # Build training dataset from data before train_cutoff_time.
            dataset = self._build_dataset(
                target, entity_type, train_cutoff_time, window_start, window_end
            )

            # Train model.
            X, y = dataset.aligned()
            if len(X) < 5:
                # Not enough training samples; skip.
                continue

            X_arr = np.array([list(f.values()) for f in X], dtype=float)
            y_arr = np.array(y, dtype=float)
            context = {
                "target": target,
                "feature_version": self._feature_version,
                "cutoff_time": train_cutoff_time,
            }
            self._model.fit(X_arr, y_arr, context)

            # Generate predictions for the upcoming Gameweek (the one this cutoff
            # represents). We use the cutoff's feature-building capability.
            pred_dataset = self._build_dataset(
                target, entity_type, cutoff.cutoff_time, window_start, window_end
            )
            pred_X, _ = pred_dataset.aligned()
            if pred_X:
                pred_X_arr = np.array([list(f.values()) for f in pred_X], dtype=float)
                preds = self._model.predict(pred_X_arr)
                for i, eid in enumerate(pred_dataset.entity_ids()):
                    if isinstance(preds, (list, np.ndarray)):
                        val = float(preds[i]) if i < len(preds) else 0.0
                    else:
                        val = float(preds)
                    all_predictions[eid] = {
                        "predicted_value": val,
                        "model_name": self._model.model_name,
                        "model_version": self._model.model_version,
                        "cutoff": cutoff.cutoff_time,
                    }

            # Persist predictions.
            prediction_records = [
                {
                    "model_name": self._model.model_name,
                    "model_version": self._model.model_version,
                    "feature_version": self._feature_version,
                    "cutoff_time": cutoff.cutoff_time,
                    "entity_type": entity_type,
                    "entity_id": eid,
                    "prediction_value": p["predicted_value"],
                }
                for eid, p in all_predictions.items()
            ]
            if prediction_records:
                self._persistence.save_predictions_batch(prediction_records)

            # Reveal actuals (for evaluation only).
            actual_dataset = self._build_outcome_dataset(target, entity_type, cutoff)
            for eid, val in actual_dataset.targets.items():
                all_actuals[eid] = {"actual_value": val}

            # Evaluate this fold.
            fold_metrics = self._evaluate_fold(all_predictions, all_actuals, cutoff)

            result = {
                "fold_index": idx - self._initial_train_gws,
                "train_cutoff": train_cutoff_time.isoformat(),
                "test_cutoff": cutoff.cutoff_time.isoformat(),
                "gameweek": cutoff.gameweek,
                "n_train_samples": len(dataset.targets),
                "n_predictions": len(all_predictions),
                **fold_metrics,
            }
            self._fold_results.append(result)

        return self._fold_results

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    @property
    def results(self) -> list[dict[str, Any]]:
        return list(self._fold_results)

    def comparison_report(self) -> ModelComparisonReport:
        """Generate a comparison report if multiple model versions exist."""
        # For a single-model walk-forward, simply aggregate.
        if not self._fold_results:
            return ModelComparisonReport()
        aggregated: dict[str, float] = {}
        for key in ("mae", "rmse"):
            values = [r.get(key, float("nan")) for r in self._fold_results]
            valid = [v for v in values if not np.isnan(v)]
            if valid:
                aggregated[key] = round(float(np.mean(valid)), 4)

        return ModelComparisonReport(
            model_names=[self._model.model_name],
            metrics={self._model.model_name: aggregated},
            n_models=1,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_train_cutoff(self, cutoffs: list[DecisionCutoff], idx: int) -> datetime:
        """Get the cutoff time to use for training data (before the current fold)."""
        train_cutoff = cutoffs[idx - 1]
        return train_cutoff.cutoff_time

    def _build_dataset(
        self,
        target: str,
        entity_type: str,
        cutoff_time: datetime,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> Any:
        """Build a training or prediction dataset."""
        if entity_type == "team":
            return self._builder.build_team_dataset(
                target=target,
                cutoff_time=cutoff_time,
                feature_version=self._feature_version,
                window_start=window_start,
                window_end=window_end,
            )
        return self._builder.build_player_dataset(
            target=target,
            cutoff_time=cutoff_time,
            feature_version=self._feature_version,
            window_start=window_start,
            window_end=window_end,
        )

    def _build_outcome_dataset(
        self,
        target: str,
        entity_type: str,
        cutoff: DecisionCutoff,
    ) -> Any:
        """Build a dataset where targets are the outcomes AFTER the cutoff."""
        return self._build_dataset(target, entity_type, cutoff.cutoff_time + timedelta(days=7))

    def _evaluate_fold(
        self,
        predictions: dict[int, dict[str, Any]],
        actuals: dict[int, dict[str, Any]],
        cutoff: DecisionCutoff,
    ) -> dict[str, float]:
        """Evaluate predictions against actuals for a single fold."""
        common = set(predictions) & set(actuals)
        if not common:
            return {"mae": float("nan"), "rmse": float("nan"), "n": 0}

        pred_vals = np.array([predictions[p]["predicted_value"] for p in common])
        actual_vals = np.array([actuals[a]["actual_value"] for a in common])

        mae = float(np.mean(np.abs(pred_vals - actual_vals)))
        rmse = float(np.sqrt(np.mean((pred_vals - actual_vals) ** 2)))

        return {
            "mae": round(mae, 4),
            "rmse": round(rmse, 4),
            "n": len(common),
        }
