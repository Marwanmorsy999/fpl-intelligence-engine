"""Base prediction model interface for the FPL Intelligence Engine.

Defines the protocol that all prediction models must implement.
This allows the backtesting engine to work with any model that
conforms to this interface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session


@runtime_checkable
class PlayerPredictionModel(Protocol):
    """Protocol for player prediction models.

    A prediction model takes features and a decision cutoff and
    produces expected points predictions for players.

    Attributes:
        model_name: Human-readable name of the model.
        model_version: Version string of the model.
    """

    @property
    def model_name(self) -> str:
        """Human-readable name of the model."""
        ...

    @property
    def model_version(self) -> str:
        """Version string of the model."""
        ...

    def fit(
        self,
        db: Session,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Fit the model on data available before the cutoff.

        Args:
            db: Database session.
            cutoff_time: The historical decision cutoff.
            context: Additional context.
        """
        ...

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Predict expected points for a single player-fixture pair.

        Args:
            player_id: The player ID.
            fixture_id: The fixture ID.
            features: Computed features for the player.
            cutoff: The decision cutoff.
            context: Additional context.

        Returns:
            Dict with 'predicted_expected_points', 'confidence',
            'data_completeness', and 'method'.
        """
        ...

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Predict for multiple players.

        Args:
            features_batch: Dict mapping player_id -> features.
            cutoff: The decision cutoff.
            context: Additional context.

        Returns:
            Dict mapping player_id -> prediction dict.
        """
        ...

    def evaluate(
        self,
        predictions: dict[int, dict[str, Any]],
        actuals: dict[int, dict[str, Any]],
    ) -> dict[str, float]:
        """Evaluate model predictions against actuals.

        Args:
            predictions: Dict mapping player_id -> prediction.
            actuals: Dict mapping player_id -> actual outcome.

        Returns:
            Dict of evaluation metrics.
        """
        ...


class PredictionModel(Protocol):
    """Full prediction-model protocol used by the Phase 4 framework.

    Extends the backtesting-facing ``PlayerPredictionModel`` with model
    lifecycle methods (``fit``, ``save``, ``load``) and metadata.

    Models must NOT own database access. Training data is supplied by
    services/repositories (e.g. the ``TrainingDataBuilder``). Models
    receive prepared feature matrices and target arrays.
    """

    @property
    def model_name(self) -> str:
        """Human-readable name of the model."""
        ...

    @property
    def model_version(self) -> str:
        """Version string of the model."""
        ...

    def metadata(self) -> dict[str, Any]:
        """Return model metadata for the registry.

        Returns:
            Dict with keys such as ``model_name``, ``model_version``,
            ``model_type``, ``feature_version``, ``hyperparameters``,
            ``random_seed``, ``created_at``.
        """
        ...

    def fit(
        self,
        X: Any,
        y: Any,
        context: dict[str, Any] | None = None,
    ) -> PredictionModel:
        """Fit the model on a prepared training dataset.

        Args:
            X: Feature matrix (numpy array / pandas DataFrame).
            y: Target array (numpy array / pandas Series).
            context: Optional context (feature_version, cutoff, etc.).

        Returns:
            The fitted model instance.
        """
        ...

    def predict(
        self,
        X: Any,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Predict for a feature matrix.

        Args:
            X: Feature matrix.
            context: Optional context.

        Returns:
            Predictions (probabilities for classifiers, floats for regressors).
        """
        ...

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Predict for multiple entities keyed by ID.

        Args:
            features_batch: Dict mapping entity_id -> feature dict.
            cutoff: The decision cutoff.
            context: Optional context.

        Returns:
            Dict mapping entity_id -> prediction dict.
        """
        ...

    def evaluate(
        self,
        predictions: Any,
        actuals: Any,
    ) -> dict[str, float]:
        """Evaluate predictions against actuals.

        Args:
            predictions: Predictions array/dict.
            actuals: Actual outcomes array/dict.

        Returns:
            Dict of evaluation metrics.
        """
        ...

    def save(self, artifact_location: str) -> str:
        """Persist the trained model artifact.

        Args:
            artifact_location: Directory path for the artifact.

        Returns:
            The concrete artifact path.
        """
        ...

    @classmethod
    def load(cls, artifact_path: str) -> PredictionModel:
        """Load a trained model artifact.

        Args:
            artifact_path: Path to the persisted artifact.

        Returns:
            The loaded model instance.
        """
        ...

