"""Market feature calculator.

Calculates historical FPL market features using only snapshots available
before the cutoff.

Features:
    - price
    - ownership
    - transfers_in
    - transfers_out
    - total_points
    - form
    - ownership_change
    - transfer_velocity
    - price_movement

Does NOT forward-fill blindly across unknown periods.
Marks data completeness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import FPLSnapshot
from fpl_intelligence.features.calculators.base import BaseFeatureCalculator
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
    apply_policy,
)


class MarketFeaturesCalculator(BaseFeatureCalculator):
    """Calculates market features using only historical snapshots."""

    @property
    def feature_name(self) -> str:
        return "market_features"

    @property
    def version(self) -> str:
        return "1.0.0"

    def compute(
        self,
        entity_id: int,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compute market features for a player at a cutoff.

        Args:
            entity_id: Player ID.
            cutoff_time: The historical decision cutoff.
            context: Must contain 'db' session and optionally 'policy'.

        Returns:
            Dict with market features and metadata.
        """
        ctx = context or {}
        db: Session = ctx.get("db")  # type: ignore[assignment]
        if db is None:
            raise ValueError("context must contain 'db' session")

        policy: InformationAccessPolicy = ctx.get("policy", DEFAULT_POLICY)

        # Query FPL snapshots before cutoff
        stmt = select(FPLSnapshot).where(
            FPLSnapshot.player_id == entity_id,
        )

        try:
            condition = apply_policy(FPLSnapshot, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass

        snapshots = list(db.execute(stmt).scalars().all())

        if not snapshots:
            return self._make_result(
                value={},
                is_missing=True,
                completeness_score=0.0,
                source_count=0,
            )

        # Sort by event_time
        snapshots_sorted = sorted(snapshots, key=lambda s: s.event_time)

        # Get the most recent snapshot before cutoff
        latest = snapshots_sorted[-1]

        value: dict[str, Any] = {}

        # Current market values
        value["price"] = latest.price
        value["ownership"] = latest.selected_by_percent
        value["transfers_in"] = latest.transfers_in_event
        value["transfers_out"] = latest.transfers_out_event
        value["total_points"] = latest.total_points
        value["form"] = latest.form
        value["points_per_game"] = latest.points_per_game

        # Calculate changes if we have multiple snapshots
        if len(snapshots_sorted) >= 2:
            prev = snapshots_sorted[-2]

            if latest.price is not None and prev.price is not None:
                value["price_movement"] = latest.price - prev.price

            if latest.selected_by_percent is not None and prev.selected_by_percent is not None:
                value["ownership_change"] = latest.selected_by_percent - prev.selected_by_percent

            if (
                latest.transfers_in_event is not None
                and latest.transfers_out_event is not None
                and prev.transfers_in_event is not None
                and prev.transfers_out_event is not None
            ):
                net_current = latest.transfers_in_event - latest.transfers_out_event
                net_prev = prev.transfers_in_event - prev.transfers_out_event
                value["transfer_velocity"] = net_current - net_prev

        # Data completeness
        # Check if we have snapshots close to the cutoff
        time_gap = None
        if latest.event_time and cutoff_time:
            event_time = latest.event_time
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=UTC)
            time_gap = (cutoff_time - event_time).total_seconds() / 3600  # hours

        # Completeness decreases with time gap
        if time_gap is not None:
            if time_gap <= 24:  # Less than 24 hours
                completeness = 1.0
            elif time_gap <= 168:  # Less than a week
                completeness = 0.8
            elif time_gap <= 720:  # Less than a month
                completeness = 0.5
            else:
                completeness = 0.2
        else:
            completeness = 0.5

        value["snapshot_count"] = len(snapshots_sorted)
        value["latest_snapshot_time"] = latest.event_time.isoformat() if latest.event_time else None
        value["time_gap_hours"] = time_gap

        return self._make_result(
            value=value,
            is_missing=False,
            completeness_score=completeness,
            source_count=len(snapshots_sorted),
            latest_source_time=latest.event_time,
        )

    def _get_all_entity_ids(
        self,
        db: Any,
        cutoff_time: datetime,
        context: dict[str, Any],
    ) -> list[int]:
        """Get all player IDs that have snapshot data before cutoff."""
        policy: InformationAccessPolicy = context.get("policy", DEFAULT_POLICY)
        stmt = select(FPLSnapshot.player_id).distinct()
        try:
            condition = apply_policy(FPLSnapshot, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass
        return list(db.execute(stmt).scalars().all())
