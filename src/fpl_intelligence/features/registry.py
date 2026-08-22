"""Feature registry for the FPL Intelligence Engine.

The FeatureRegistry manages feature calculators, their definitions,
and provides a unified interface for computing features as-of a cutoff.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.features.cache import FeatureCache
from fpl_intelligence.features.calculators.base import BaseFeatureCalculator
from fpl_intelligence.features.models import FeatureDefinition, FeatureSnapshot
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
)

logger = logging.getLogger(__name__)


class FeatureRegistry:
    """Registry for feature calculators and definitions.

    Manages:
        - Registration of feature calculators
        - Feature definitions (metadata)
        - Caching of computed features
        - Batch computation across entities
    """

    def __init__(self, db: Session, cache: FeatureCache | None = None) -> None:
        self._db = db
        self._cache = cache or FeatureCache()
        self._calculators: dict[str, BaseFeatureCalculator] = {}
        self._definitions: dict[str, FeatureDefinition] = {}

    def register(self, calculator: BaseFeatureCalculator) -> None:
        """Register a feature calculator.

        Args:
            calculator: A feature calculator instance.
        """
        name = calculator.feature_name
        version = calculator.version
        self._calculators[name] = calculator

        # Register or update the feature definition
        existing = self._db.scalar(
            select(FeatureDefinition).where(
                FeatureDefinition.feature_name == name,
                FeatureDefinition.version == version,
            )
        )
        if existing is None:
            definition = FeatureDefinition(
                feature_name=name,
                description=f"Feature set: {name}",
                data_type="json",
                entity_type="player",
                version=version,
                calculation_method=calculator.__class__.__name__,
            )
            self._db.add(definition)
            self._db.flush()
            self._definitions[name] = definition
            logger.info("Registered feature definition: %s v%s", name, version)
        else:
            self._definitions[name] = existing

    def get(self, feature_name: str) -> BaseFeatureCalculator:
        """Get a registered calculator by feature name.

        Args:
            feature_name: The canonical feature name.

        Returns:
            The registered calculator.

        Raises:
            KeyError: If the feature is not registered.
        """
        if feature_name not in self._calculators:
            raise KeyError(f"Feature '{feature_name}' is not registered.")
        return self._calculators[feature_name]

    def list_features(self) -> list[str]:
        """Return a list of all registered feature names."""
        return list(self._calculators.keys())

    def compute(
        self,
        feature_name: str,
        entity_id: int,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Compute a single feature for an entity at a cutoff.

        Args:
            feature_name: The canonical feature name.
            entity_id: The entity ID.
            cutoff_time: The historical decision cutoff.
            context: Additional context (e.g. db session, policy).
            use_cache: Whether to use the feature cache.

        Returns:
            Feature result dict with 'value', 'is_missing', etc.
        """
        calculator = self.get(feature_name)
        ctx = context or {}
        ctx.setdefault("db", self._db)
        ctx.setdefault("policy", DEFAULT_POLICY)

        if use_cache:
            cached = self._cache.get(
                feature_name,
                calculator.version,
                entity_id,
                cutoff_time,
            )
            if cached is not None:
                return cached

        result = calculator.compute(entity_id, cutoff_time, ctx)

        if use_cache:
            self._cache.set(
                feature_name,
                calculator.version,
                entity_id,
                cutoff_time,
                result,
            )

        # Persist snapshot
        snapshot = FeatureSnapshot(
            entity_id=entity_id,
            feature_name=feature_name,
            feature_version=calculator.version,
            cutoff_time=cutoff_time,
            value=result.get("value"),
            is_missing=result.get("is_missing", False),
            completeness_score=result.get("completeness_score"),
            source_count=result.get("source_count"),
            latest_source_time=result.get("latest_source_time"),
        )
        self._db.add(snapshot)
        self._db.flush()

        return result

    def compute_all(
        self,
        entity_id: int,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """Compute all registered features for an entity at a cutoff.

        Args:
            entity_id: The entity ID.
            cutoff_time: The historical decision cutoff.
            context: Additional context.
            use_cache: Whether to use the feature cache.

        Returns:
            Dict mapping feature_name -> result dict.
        """
        results: dict[str, dict[str, Any]] = {}
        for name in self._calculators:
            results[name] = self.compute(name, entity_id, cutoff_time, context, use_cache)
        return results

    def compute_batch(
        self,
        feature_name: str,
        entity_ids: list[int],
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> dict[int, dict[str, Any]]:
        """Compute a feature for multiple entities at a cutoff.

        Args:
            feature_name: The canonical feature name.
            entity_ids: List of entity IDs.
            cutoff_time: The historical decision cutoff.
            context: Additional context.
            use_cache: Whether to use the feature cache.

        Returns:
            Dict mapping entity_id -> result dict.
        """
        results: dict[int, dict[str, Any]] = {}
        for entity_id in entity_ids:
            results[entity_id] = self.compute(
                feature_name, entity_id, cutoff_time, context, use_cache
            )
        return results

    def compute_features(
        self,
        db_session: Session,
        cutoff: Any,
        player_ids: list[int] | None = None,
    ) -> dict[int, dict[str, float]]:
        """Compute features for all players at a cutoff.

        This is the interface expected by the BacktestEngine.

        Args:
            db_session: Database session.
            cutoff: A DecisionCutoff object with cutoff_time and policy.
            player_ids: Optional list of player IDs to compute for.

        Returns:
            Dict mapping player_id -> feature dict (flattened values).
        """
        cutoff_time = cutoff.cutoff_time
        policy = getattr(cutoff, "policy", DEFAULT_POLICY)

        ctx = {"db": db_session, "policy": policy}

        if player_ids is None:
            # Get all player IDs from the first calculator
            calculator = next(iter(self._calculators.values()))
            player_ids = calculator.get_all_entity_ids(db_session, cutoff_time, ctx)

        results: dict[int, dict[str, float]] = {}
        for player_id in player_ids:
            all_features = self.compute_all(player_id, cutoff_time, ctx)
            # Flatten: merge all feature values into a single dict
            flat: dict[str, float] = {}
            for feat_name, result in all_features.items():
                value = result.get("value", {})
                if isinstance(value, dict):
                    for k, v in value.items():
                        if isinstance(v, (int, float)):
                            flat[f"{feat_name}_{k}"] = float(v)
            results[player_id] = flat

        return results
