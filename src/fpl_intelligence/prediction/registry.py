"""Model registry for versioning and persisting prediction model artifacts.

Provides an abstraction layer over the ``model_registry`` database table
and the file-system artifact store.

Supports:

- ``save(model, db)``: Register a model and persist its artifact.
- ``load(model_name, version=None)``: Load a registered model.
- ``promote(model_name, version)``: Promote a model to ``active``.
- ``retire(model_name, version)``: Retire a model.
- ``list_models()``: List registered models.

Artifacts are stored under a configurable base directory
(``data/models/`` by default). Metadata is persisted to PostgreSQL.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from fpl_intelligence.prediction.base import PredictionModel
from fpl_intelligence.prediction.models import ModelRegistryEntry

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT_DIR = os.path.join("data", "models")


class ModelRegistry:
    """Registry for model versioning and artifact management.

    Args:
        db: Database session.
        artifact_dir: Base directory for model artifacts.
    """

    def __init__(
        self,
        db: Session,
        artifact_dir: str | None = None,
    ) -> None:
        self._db = db
        self._artifact_dir = Path(artifact_dir or DEFAULT_ARTIFACT_DIR)
        self._artifact_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        model: PredictionModel,
        feature_version: str | None = None,
        training_cutoff: datetime | None = None,
        training_start: datetime | None = None,
        training_end: datetime | None = None,
        metrics: dict[str, Any] | None = None,
        hyperparameters: dict[str, Any] | None = None,
        random_seed: int | None = None,
        training_sample_count: int | None = None,
    ) -> ModelRegistryEntry:
        """Register a trained model in the registry.

        The model is saved to disk under ``artifact_dir/model_name/version/``.
        A corresponding entry is created in the ``model_registry`` table.

        Args:
            model: The trained model instance.
            feature_version: Feature-store version used.
            training_cutoff: The decision cutoff for training.
            training_start: Start of training window.
            training_end: End of training window.
            metrics: Evaluation metrics.
            hyperparameters: Model hyperparameters.
            random_seed: Seed used during training.
            training_sample_count: Number of training rows.

        Returns:
            The ``ModelRegistryEntry``.
        """
        meta = model.metadata()
        model_name = meta.get("model_name", model.model_name)
        model_version = meta.get("model_version", model.model_version)

        # Check for existing entry.
        existing = self._db.scalar(
            select(ModelRegistryEntry).where(
                ModelRegistryEntry.model_name == model_name,
                ModelRegistryEntry.model_version == model_version,
            )
        )
        if existing is not None:
            raise ValueError(f"Model {model_name} v{model_version} is already registered.")

        # Persist artifact.
        artifact_location = str(self._artifact_dir / model_name / model_version)
        artifact_path = model.save(artifact_location)

        # Create DB entry.
        entry = ModelRegistryEntry(
            model_name=model_name,
            model_version=model_version,
            model_type=meta.get("model_type", "unknown"),
            feature_version=feature_version or meta.get("feature_version"),
            training_cutoff=training_cutoff,
            training_start=training_start,
            training_end=training_end,
            hyperparameters=hyperparameters or meta.get("hyperparameters"),
            random_seed=random_seed or meta.get("random_seed"),
            training_sample_count=training_sample_count,
            metrics=metrics,
            artifact_location=artifact_path,
            status="staged",
            created_at=datetime.now(UTC),
        )
        self._db.add(entry)
        self._db.flush()
        logger.info("Registered model: %s v%s -> %s", model_name, model_version, artifact_path)
        return entry

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(
        self,
        model_name: str,
        model_version: str | None = None,
        status: str | None = "active",
    ) -> PredictionModel:
        """Load a registered model.

        Args:
            model_name: Canonical model name.
            model_version: Specific version. If ``None``, the latest
                with the given status is loaded.
            status: Status filter (default ``active``). Use ``None`` to
                search across all statuses.

        Returns:
            The loaded ``PredictionModel`` instance.

        Raises:
            ValueError: If no matching model is found.
        """
        stmt = select(ModelRegistryEntry).where(
            ModelRegistryEntry.model_name == model_name,
        )
        if model_version is not None:
            stmt = stmt.where(ModelRegistryEntry.model_version == model_version)
        if status is not None:
            stmt = stmt.where(ModelRegistryEntry.status == status)
        stmt = stmt.order_by(ModelRegistryEntry.created_at.desc()).limit(1)

        entry = self._db.scalar(stmt)
        if entry is None:
            raise ValueError(
                f"No registered model '{model_name}' (version={model_version}, status={status})"
            )
        if entry.artifact_location is None:
            raise ValueError(f"Model '{model_name}' v{entry.model_version} has no artifact.")
        return self._load_artifact(entry.artifact_location)

    @staticmethod
    def _load_artifact(artifact_path: str) -> PredictionModel:
        """Load a model from an artifact path using its class's ``load``."""
        # Inspection: try importing known model classes.
        from fpl_intelligence.prediction.baselines import (
            FixtureAdjustedBaselineModel,
            MinutesAdjustedBaselineModel,
            RecentFormBaselineModel,
        )
        from fpl_intelligence.prediction.minutes import MinutesModel

        for cls in [
            MinutesModel,
            RecentFormBaselineModel,
            MinutesAdjustedBaselineModel,
            FixtureAdjustedBaselineModel,
        ]:
            try:
                return cls.load(artifact_path)  # type: ignore[attr-defined]
            except Exception:
                continue

        # Fallback: try dynamic import based on artifact metadata.
        meta_path = str(Path(artifact_path).with_suffix(".json"))
        if Path(meta_path).exists():
            meta = json.loads(Path(meta_path).read_text())
            model_type = meta.get("model_type", "")
            if "minutes" in model_type.lower():
                return MinutesModel.load(artifact_path)

        raise ValueError(f"Could not load artifact: {artifact_path}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def promote(self, model_name: str, model_version: str) -> ModelRegistryEntry:
        """Promote a model to ``active`` status.

        Demotes any other active model with the same name.

        Args:
            model_name: Canonical model name.
            model_version: Version to promote.

        Returns:
            The promoted entry.
        """
        # Demote existing active models.
        self._db.execute(
            update(ModelRegistryEntry)
            .where(
                ModelRegistryEntry.model_name == model_name,
                ModelRegistryEntry.status == "active",
            )
            .values(status="staged")
        )
        # Promote requested.
        entry = self._get_entry(model_name, model_version)
        entry.status = "active"
        self._db.flush()
        logger.info("Promoted model: %s v%s -> active", model_name, model_version)
        return entry

    def retire(self, model_name: str, model_version: str) -> ModelRegistryEntry:
        """Retire a model (set status to ``retired``)."""
        entry = self._get_entry(model_name, model_version)
        entry.status = "retired"
        self._db.flush()
        logger.info("Retired model: %s v%s", model_name, model_version)
        return entry

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def list_models(
        self,
        model_name: str | None = None,
        status: str | None = None,
    ) -> list[ModelRegistryEntry]:
        """List registered models.

        Args:
            model_name: Optional name filter.
            status: Optional status filter.

        Returns:
            List of ``ModelRegistryEntry``.
        """
        stmt = select(ModelRegistryEntry)
        if model_name is not None:
            stmt = stmt.where(ModelRegistryEntry.model_name == model_name)
        if status is not None:
            stmt = stmt.where(ModelRegistryEntry.status == status)
        stmt = stmt.order_by(ModelRegistryEntry.created_at.desc())
        return list(self._db.execute(stmt).scalars().all())

    def get_active(self, model_name: str) -> ModelRegistryEntry | None:
        """Get the currently active version of a model."""
        return self._db.scalar(
            select(ModelRegistryEntry).where(
                ModelRegistryEntry.model_name == model_name,
                ModelRegistryEntry.status == "active",
            )
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_entry(self, model_name: str, model_version: str) -> ModelRegistryEntry:
        entry = self._db.scalar(
            select(ModelRegistryEntry).where(
                ModelRegistryEntry.model_name == model_name,
                ModelRegistryEntry.model_version == model_version,
            )
        )
        if entry is None:
            raise ValueError(f"Model '{model_name}' v{model_version} not found in registry.")
        return entry
