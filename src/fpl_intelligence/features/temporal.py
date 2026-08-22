"""Temporal query helpers for no-look-ahead enforcement.

This module provides the core abstraction for ensuring that feature
calculations and backtesting queries never access data that was not
available at the historical decision cutoff.

Key concepts:
    - event_time: When the football event occurred.
    - published_at: When a source published the information.
    - available_at: The earliest timestamp at which our system can
      legitimately be considered able to use the information.
    - ingested_at: When our pipeline actually collected it.
    - source_last_modified_at: When the source last modified the record.

The backtester must reason about `available_at`, not merely `published_at`.

Information-access policies:
    - PUBLIC_AVAILABILITY: Use information if available_at <= cutoff.
    - SYSTEM_AVAILABILITY: Use information if ingested_at <= cutoff.
    - STRICT_REPRODUCIBILITY: Use information only if both
      available_at <= cutoff AND ingested_at <= cutoff. (default)
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar

from sqlalchemy import and_
from sqlalchemy.orm import Session
from sqlalchemy.sql import select
from sqlalchemy.sql.elements import ColumnElement


class InformationAccessPolicy(StrEnum):
    """Policy for determining what information is available at a cutoff.

    PUBLIC_AVAILABILITY:
        Use information if available_at <= cutoff.
        This assumes the system could have accessed any publicly available
        information, even if it was not actually ingested.

    SYSTEM_AVAILABILITY:
        Use information if ingested_at <= cutoff.
        This uses only information that our pipeline actually collected
        before the cutoff, regardless of when it was published.

    STRICT_REPRODUCIBILITY:
        Use information only if both available_at <= cutoff AND
        ingested_at <= cutoff. This is the strictest and most conservative
        policy, ensuring that the backtest only uses information that was
        both publicly available and actually in our system.
    """

    PUBLIC_AVAILABILITY = "public_availability"
    SYSTEM_AVAILABILITY = "system_availability"
    STRICT_REPRODUCIBILITY = "strict_reproducibility"


# Default policy for all backtesting
DEFAULT_POLICY = InformationAccessPolicy.STRICT_REPRODUCIBILITY


T = TypeVar("T")


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is timezone-aware.

    SQLite (and some other backends) may return naive datetimes even when
    the column is declared as ``DateTime(timezone=True)``.  This helper
    attaches UTC if the datetime is naive so that comparisons with
    timezone-aware cutoffs do not raise ``TypeError``.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def as_of(cutoff_time: datetime, column: ColumnElement) -> ColumnElement:
    """Return a filter condition for 'as of' a cutoff time.

    This is the basic temporal filter that ensures a record's timestamp
    is before or at the cutoff.

    Args:
        cutoff_time: The historical decision cutoff.
        column: The SQLAlchemy column to filter on.

    Returns:
        A SQLAlchemy filter condition.
    """
    return column <= cutoff_time


def apply_policy(
    model: type,
    policy: InformationAccessPolicy,
    cutoff_time: datetime,
) -> ColumnElement:
    """Apply an information-access policy to a model.

    Returns a SQLAlchemy filter condition that enforces the policy.

    Args:
        model: The SQLAlchemy model class.
        policy: The information-access policy.
        cutoff_time: The historical decision cutoff.

    Returns:
        A SQLAlchemy filter condition.

    Raises:
        ValueError: If the model lacks the required temporal columns.
    """
    if policy == InformationAccessPolicy.PUBLIC_AVAILABILITY:
        # Use available_at if it exists, otherwise fall back to published_at
        if hasattr(model, "available_at"):
            col = model.available_at
            return col <= cutoff_time
        if hasattr(model, "published_at"):
            col = model.published_at
            return col <= cutoff_time
        # Fall back to event_time
        if hasattr(model, "event_time"):
            col = model.event_time
            return col <= cutoff_time
        raise ValueError(
            f"Model {model.__name__} has no available_at, published_at, or event_time column"
        )

    if policy == InformationAccessPolicy.SYSTEM_AVAILABILITY:
        if hasattr(model, "ingested_at"):
            col = model.ingested_at
            return col <= cutoff_time
        raise ValueError(f"Model {model.__name__} has no ingested_at column")

    if policy == InformationAccessPolicy.STRICT_REPRODUCIBILITY:
        conditions = []
        if hasattr(model, "available_at"):
            conditions.append(model.available_at <= cutoff_time)
        elif hasattr(model, "published_at"):
            conditions.append(model.published_at <= cutoff_time)
        elif hasattr(model, "event_time"):
            conditions.append(model.event_time <= cutoff_time)
        else:
            raise ValueError(
                f"Model {model.__name__} has no available_at, published_at, or event_time column"
            )

        if hasattr(model, "ingested_at"):
            conditions.append(model.ingested_at <= cutoff_time)
        else:
            raise ValueError(f"Model {model.__name__} has no ingested_at column")

        return and_(*conditions)

    raise ValueError(f"Unknown policy: {policy}")


class TemporalQueryBuilder:
    """Wraps a SQLAlchemy query with temporal cutoff filters.

    This ensures that all queries respect the no-look-ahead rule.

    Usage:
        cutoff = datetime(2025, 1, 15, tzinfo=UTC)
        builder = TemporalQueryBuilder(db, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY)
        results = builder.query(PlayerGameweekPerformance).filter(
            PlayerGameweekPerformance.player_id == 123
        ).all()
    """

    def __init__(
        self,
        db: Session,
        cutoff_time: datetime,
        policy: InformationAccessPolicy = DEFAULT_POLICY,
    ) -> None:
        self._db = db
        self._cutoff = cutoff_time
        self._policy = policy

    @property
    def cutoff(self) -> datetime:
        return self._cutoff

    @property
    def policy(self) -> InformationAccessPolicy:
        return self._policy

    def query(self, model: type[T]) -> Any:
        """Create a query for the given model with temporal filters applied.

        Args:
            model: The SQLAlchemy model class to query.

        Returns:
            A SQLAlchemy query with temporal filters applied.
        """
        stmt = select(model)
        try:
            condition = apply_policy(model, self._policy, self._cutoff)
            stmt = stmt.where(condition)
        except ValueError:
            # Model has no temporal columns; query without filter
            pass
        return self._db.execute(stmt).scalars()

    def query_with_filter(self, model: type[T], *filters: ColumnElement) -> list[T]:
        """Query with additional filters beyond the temporal cutoff.

        Args:
            model: The SQLAlchemy model class to query.
            filters: Additional SQLAlchemy filter conditions.

        Returns:
            List of model instances.
        """
        stmt = select(model)
        try:
            condition = apply_policy(model, self._policy, self._cutoff)
            stmt = stmt.where(condition)
        except ValueError:
            pass
        for f in filters:
            stmt = stmt.where(f)
        return list(self._db.execute(stmt).scalars().all())


def is_record_available(
    record: Any,
    cutoff_time: datetime,
    policy: InformationAccessPolicy = DEFAULT_POLICY,
) -> bool:
    """Check if a single record is available under the given policy.

    Args:
        record: A model instance with temporal fields.
        cutoff_time: The historical decision cutoff.
        policy: The information-access policy.

    Returns:
        True if the record is available under the policy.
    """
    available_at = _ensure_aware(getattr(record, "available_at", None))
    published_at = _ensure_aware(getattr(record, "published_at", None))
    ingested_at = _ensure_aware(getattr(record, "ingested_at", None))
    event_time = _ensure_aware(getattr(record, "event_time", None))

    if policy == InformationAccessPolicy.PUBLIC_AVAILABILITY:
        check_time = available_at or published_at or event_time
        if check_time is None:
            return False
        return check_time <= cutoff_time

    if policy == InformationAccessPolicy.SYSTEM_AVAILABILITY:
        if ingested_at is None:
            return False
        return ingested_at <= cutoff_time

    if policy == InformationAccessPolicy.STRICT_REPRODUCIBILITY:
        check_time = available_at or published_at or event_time
        if check_time is None or ingested_at is None:
            return False
        return check_time <= cutoff_time and ingested_at <= cutoff_time

    return False
