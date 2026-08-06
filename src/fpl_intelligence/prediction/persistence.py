"""Immutable prediction persistence.

Persists model predictions to the ``model_predictions`` table.

Key rules:
- Each prediction is associated with a player/team/fixture, decision cutoff,
  feature version, model version, prediction timestamp, prediction value,
  uncertainty, and data completeness.
- Old predictions are NEVER overwritten. They are immutable records.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.prediction.models import ModelPrediction


class PredictionPersistence:
    """Persists and retrieves immutable model predictions.

    Args:
        db: Database session.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_prediction(
        self,
        model_name: str,
        model_version: str,
        feature_version: str | None,
        cutoff_time: datetime,
        entity_type: str,
        entity_id: int,
        prediction_value: float | None,
        prediction_lower: float | None = None,
        prediction_upper: float | None = None,
        prediction_data: dict[str, Any] | None = None,
        confidence: float | None = None,
        data_completeness: float | None = None,
    ) -> ModelPrediction:
        """Save a single immutable prediction.

        Args:
            model_name: Model that produced the prediction.
            model_version: Version of the model.
            feature_version: Feature-store version used.
            cutoff_time: The decision cutoff.
            entity_type: ``player``, ``team``, or ``fixture``.
            entity_id: ID of the entity.
            prediction_value: The primary prediction value.
            prediction_lower: Lower bound estimate.
            prediction_upper: Upper bound estimate.
            prediction_data: Additional prediction outputs (JSON).
            confidence: Optional confidence/calibration measure.
            data_completeness: Explainable 0-1 completeness score.

        Returns:
            The new ``ModelPrediction`` record (already flushed).
        """
        record = ModelPrediction(
            model_name=model_name,
            model_version=model_version,
            feature_version=feature_version,
            cutoff_time=cutoff_time,
            entity_type=entity_type,
            entity_id=entity_id,
            prediction_value=prediction_value,
            prediction_lower=prediction_lower,
            prediction_upper=prediction_upper,
            prediction_data=prediction_data,
            confidence=confidence,
            data_completeness=data_completeness,
            prediction_timestamp=datetime.now(UTC),
            is_frozen=True,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def save_predictions_batch(
        self,
        predictions: list[dict[str, Any]],
    ) -> list[ModelPrediction]:
        """Save multiple predictions in a single batch.

        Args:
            predictions: List of prediction dicts. Each must contain
                the keys required by ``save_prediction``.

        Returns:
            List of created ``ModelPrediction`` records.
        """
        records: list[ModelPrediction] = []
        for pred in predictions:
            record = self.save_prediction(
                model_name=pred["model_name"],
                model_version=pred["model_version"],
                feature_version=pred.get("feature_version"),
                cutoff_time=pred["cutoff_time"],
                entity_type=pred["entity_type"],
                entity_id=pred["entity_id"],
                prediction_value=pred.get("prediction_value"),
                prediction_lower=pred.get("prediction_lower"),
                prediction_upper=pred.get("prediction_upper"),
                prediction_data=pred.get("prediction_data"),
                confidence=pred.get("confidence"),
                data_completeness=pred.get("data_completeness"),
            )
            records.append(record)
        return records

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_predictions(
        self,
        model_name: str | None = None,
        model_version: str | None = None,
        entity_type: str | None = None,
        entity_id: int | None = None,
        cutoff_time: datetime | None = None,
        limit: int = 100,
    ) -> list[ModelPrediction]:
        """Retrieve predictions with optional filters.

        Args:
            model_name: Filter by model name.
            model_version: Filter by model version.
            entity_type: Filter by entity type.
            entity_id: Filter by entity ID.
            cutoff_time: Filter by cutoff time (records at or before).
            limit: Maximum number of records to return.

        Returns:
            List of ``ModelPrediction`` records.
        """
        stmt = select(ModelPrediction)
        if model_name is not None:
            stmt = stmt.where(ModelPrediction.model_name == model_name)
        if model_version is not None:
            stmt = stmt.where(ModelPrediction.model_version == model_version)
        if entity_type is not None:
            stmt = stmt.where(ModelPrediction.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(ModelPrediction.entity_id == entity_id)
        if cutoff_time is not None:
            stmt = stmt.where(ModelPrediction.cutoff_time <= cutoff_time)
        stmt = stmt.order_by(ModelPrediction.cutoff_time.desc()).limit(limit)
        return list(self._db.execute(stmt).scalars().all())

    def get_prediction_by_run(
        self,
        run_id: int,
        entity_id: int,
        entity_type: str = "player",
    ) -> list[ModelPrediction]:
        """Retrieve predictions for a specific entity in a backtest run.

        Args:
            run_id: Backtest run ID.
            entity_id: Entity ID.
            entity_type: Entity type (default ``player``).

        Returns:
            List of ``ModelPrediction`` records.
        """
        # Backtest runs store predictions in their own PlayerPrediction table.
        # This method bridges the two systems.
        from fpl_intelligence.backtesting.models import PlayerPrediction

        stmt = (
            select(PlayerPrediction)
            .where(
                PlayerPrediction.run_id == run_id,
                PlayerPrediction.player_id == entity_id,
            )
        )
        backtest_preds = list(self._db.execute(stmt).scalars().all())

        output: list[ModelPrediction] = []
        for bp in backtest_preds:
            record = ModelPrediction(
                model_name="backtest",
                model_version=bp.model_version,
                cutoff_time=bp.cutoff,
                entity_type="player",
                entity_id=bp.player_id,
                prediction_value=bp.predicted_expected_points,
                prediction_lower=bp.prediction_interval_lower,
                prediction_upper=bp.prediction_interval_upper,
                prediction_timestamp=bp.created_at,
                is_frozen=bp.is_frozen,
            )
            output.append(record)
        return output

