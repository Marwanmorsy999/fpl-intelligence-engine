"""Player availability feature calculator.

If historical availability data exists, supports:
    - injury_status
    - suspension_status
    - availability_status

If the repository does not yet have trustworthy historical injury data:
    - leaves the feature null
    - records missingness
    - documents the limitation

Does NOT create synthetic injury labels unless explicitly marked as synthetic test data.
"""

from __future__ import annotations

from datetime import datetime
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


class PlayerAvailabilityCalculator(BaseFeatureCalculator):
    """Calculates player availability features.

    LIMITATION: The repository does not yet have trustworthy historical
    injury data. This calculator returns null features with is_missing=True
    and documents the limitation.

    When injury data becomes available, this calculator should be updated
    to query the injury data source and compute:
        - injury_status: "fit", "doubtful", "injured", "suspended"
        - suspension_status: "available", "suspended", "at_risk"
        - availability_status: "available", "unlikely", "ruled_out"
    """

    @property
    def feature_name(self) -> str:
        return "player_availability"

    @property
    def version(self) -> str:
        return "1.0.0"

    def compute(
        self,
        entity_id: int,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compute player availability features for a player at a cutoff.

        Args:
            entity_id: Player ID.
            cutoff_time: The historical decision cutoff.
            context: Must contain 'db' session and optionally 'policy'.

        Returns:
            Dict with availability features and metadata.
            All features will be null with is_missing=True until
            historical injury data is available.
        """
        ctx = context or {}
        db: Session = ctx.get("db")  # type: ignore[assignment]
        if db is None:
            raise ValueError("context must contain 'db' session")

        policy: InformationAccessPolicy = ctx.get("policy", DEFAULT_POLICY)

        # Check if we have any FPL snapshots (which may contain availability info)
        stmt = select(FPLSnapshot).where(
            FPLSnapshot.player_id == entity_id,
        )
        try:
            condition = apply_policy(FPLSnapshot, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass

        snapshots = list(db.execute(stmt).scalars().all())

        # Check if any snapshot has availability-related fields
        # Currently, FPL snapshots don't have injury/suspension data
        # This is a known limitation

        value: dict[str, Any] = {
            "injury_status": None,
            "suspension_status": None,
            "availability_status": None,
            "has_historical_injury_data": False,
            "limitation": (
                "The repository does not yet have trustworthy historical "
                "injury data. Availability features are null. "
                "Do not create synthetic injury labels unless explicitly "
                "marked as synthetic test data."
            ),
        }

        # If we have snapshots, we can infer basic availability from
        # whether the player was selected/playing
        if snapshots:
            latest = max(snapshots, key=lambda s: s.event_time)
            value["inferred_from_snapshots"] = True
            value["latest_snapshot_time"] = latest.event_time.isoformat() if latest.event_time else None
            # We cannot infer injury status from FPL snapshots alone
            # This would require a dedicated injury data source

        return self._make_result(
            value=value,
            is_missing=True,  # Always missing until injury data is available
            completeness_score=0.0,
            source_count=len(snapshots) if snapshots else 0,
            latest_source_time=snapshots[-1].event_time if snapshots else None,
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