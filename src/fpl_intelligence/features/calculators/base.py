"""Base classes and protocol for feature calculators.

A FeatureCalculator computes a set of features for a specific entity type
at a given historical cutoff time. Calculators must respect the no-look-ahead
rule by using temporal query helpers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FeatureCalculator(Protocol):
    """Protocol for feature calculators.

    A feature calculator computes features for a specific entity type
    at a given historical cutoff time.

    Attributes:
        feature_name: Canonical name of the feature set.
        version: Semantic version of the calculation logic.
    """

    @property
    def feature_name(self) -> str:
        """Canonical name of the feature set, e.g. 'player_form'."""
        ...

    @property
    def version(self) -> str:
        """Semantic version of the calculation logic, e.g. '1.0.0'."""
        ...

    def compute(
        self,
        entity_id: int,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compute features for an entity at a cutoff time.

        Args:
            entity_id: ID of the entity (player, team, fixture).
            cutoff_time: The historical decision cutoff.
            context: Additional context (e.g. db session, policy).

        Returns:
            Dict with 'value', 'is_missing', 'completeness_score',
            'source_count', and 'latest_source_time'.
        """
        ...

    def get_all_entity_ids(
        self,
        db: Any,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
    ) -> list[int]:
        """Get all entity IDs that have data before the cutoff.

        Args:
            db: Database session.
            cutoff_time: The historical decision cutoff.
            context: Additional context.

        Returns:
            List of entity IDs.
        """
        ...


class BaseFeatureCalculator:
    """Base class providing common functionality for feature calculators.

    Subclasses must implement `feature_name`, `version`, and `compute`.
    """

    @property
    def feature_name(self) -> str:
        raise NotImplementedError

    @property
    def version(self) -> str:
        raise NotImplementedError

    def compute(
        self,
        entity_id: int,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _get_all_entity_ids(
        self,
        db: Any,
        cutoff_time: datetime,
        context: dict[str, Any],
    ) -> list[int]:
        """Override in subclass to provide entity IDs for batch computation."""
        return []

    def get_all_entity_ids(
        self,
        db: Any,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
    ) -> list[int]:
        """Get all entity IDs that have data before the cutoff."""
        return self._get_all_entity_ids(db, cutoff_time, context or {})

    def _make_result(
        self,
        value: dict[str, Any] | None,
        is_missing: bool,
        completeness_score: float,
        source_count: int,
        latest_source_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Build a standardized feature result dict.

        Args:
            value: The computed feature values.
            is_missing: Whether the feature value is missing.
            completeness_score: 0.0 to 1.0 indicating data completeness.
            source_count: Number of source records used.
            latest_source_time: Timestamp of the most recent source record.

        Returns:
            Standardized result dict with metadata.
        """
        return {
            "value": value,
            "is_missing": is_missing,
            "completeness_score": completeness_score,
            "source_count": source_count,
            "latest_source_time": latest_source_time,
        }
