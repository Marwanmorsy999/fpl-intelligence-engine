"""Decision cutoff model for the FPL Intelligence Engine.

A DecisionCutoff represents the point in time at which a prediction
decision must be made, along with the information-access policy that
governs what data is available at that point.

The cutoff is derived from the Gameweek deadline time, adjusted by
a configurable offset to simulate realistic decision-making.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import Gameweek
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
    _ensure_aware,
)


@dataclass(frozen=True)
class DecisionCutoff:
    """Represents a decision point in time for backtesting.

    Attributes:
        cutoff_time: The timestamp before which all data must be available.
        gameweek: The gameweek number this cutoff applies to.
        season: The season code.
        policy: The information-access policy to enforce.
        deadline_time: The original FPL deadline time.
        offset: How far before the deadline the decision is made.
    """

    cutoff_time: datetime
    gameweek: int
    season: str
    policy: InformationAccessPolicy = field(default=DEFAULT_POLICY)
    deadline_time: datetime | None = None
    offset: timedelta | None = None

    def __post_init__(self) -> None:
        if self.cutoff_time.tzinfo is None:
            raise ValueError("cutoff_time must be timezone-aware")

    @property
    def is_strict(self) -> bool:
        """Whether this cutoff uses strict reproducibility policy."""
        return self.policy == InformationAccessPolicy.STRICT_REPRODUCIBILITY


def get_gameweek_decision_cutoff(
    db: Session,
    season: str,
    gameweek: int,
    policy: InformationAccessPolicy = DEFAULT_POLICY,
    offset: timedelta | None = None,
) -> DecisionCutoff:
    """Compute the decision cutoff for a specific gameweek.

    The cutoff is derived from the gameweek deadline time, adjusted
    by an optional offset (e.g., to simulate making decisions 1 hour
    before the deadline).

    Args:
        db: Database session.
        season: Season code (e.g., "2025-26").
        gameweek: Gameweek number.
        policy: Information-access policy.
        offset: Optional offset from the deadline. If None, uses the
            deadline time directly.

    Returns:
        A DecisionCutoff for the given gameweek.

    Raises:
        ValueError: If the gameweek is not found.
    """
    from fpl_intelligence.db.models import Season

    stmt = (
        select(Gameweek)
        .join(Season)
        .where(Season.code == season, Gameweek.provider_event_id == gameweek)
    )
    gw = db.scalar(stmt)
    if gw is None:
        raise ValueError(f"Gameweek {gameweek} for season {season!r} not found.")

    deadline = gw.deadline_time
    if deadline is None:
        raise ValueError(f"Gameweek {gameweek} for season {season!r} has no deadline_time.")

    if offset is None:
        offset = timedelta(hours=1)  # Default: decide 1 hour before deadline

    cutoff_time = _ensure_aware(deadline - offset)
    assert cutoff_time is not None

    return DecisionCutoff(
        cutoff_time=cutoff_time,
        gameweek=gameweek,
        season=season,
        policy=policy,
        deadline_time=_ensure_aware(deadline),
        offset=offset,
    )


def get_all_gameweek_cutoffs(
    db: Session,
    season: str,
    start_gameweek: int = 1,
    end_gameweek: int | None = None,
    policy: InformationAccessPolicy = DEFAULT_POLICY,
    offset: timedelta | None = None,
) -> list[DecisionCutoff]:
    """Compute decision cutoffs for a range of gameweeks.

    Args:
        db: Database session.
        season: Season code.
        start_gameweek: First gameweek to include.
        end_gameweek: Last gameweek to include (inclusive). If None,
            includes all gameweeks from start_gameweek onward.
        policy: Information-access policy.
        offset: Optional offset from the deadline.

    Returns:
        List of DecisionCutoff objects, ordered by gameweek.
    """
    from fpl_intelligence.db.models import Season

    stmt = (
        select(Gameweek)
        .join(Season)
        .where(Season.code == season, Gameweek.provider_event_id >= start_gameweek)
    )
    if end_gameweek is not None:
        stmt = stmt.where(Gameweek.provider_event_id <= end_gameweek)
    stmt = stmt.order_by(Gameweek.provider_event_id)

    gameweeks = db.scalars(stmt).all()

    cutoffs: list[DecisionCutoff] = []
    for gw in gameweeks:
        if gw.deadline_time is None:
            continue
        gw_offset = timedelta(hours=1) if offset is None else offset
        cutoff_time = _ensure_aware(gw.deadline_time - gw_offset)
        assert cutoff_time is not None
        cutoffs.append(
            DecisionCutoff(
                cutoff_time=cutoff_time,
                gameweek=gw.provider_event_id,
                season=season,
                policy=policy,
                deadline_time=_ensure_aware(gw.deadline_time),
                offset=gw_offset,
            )
        )

    return cutoffs
