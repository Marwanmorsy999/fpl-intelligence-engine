"""SQLAlchemy models for the feature store.

These tables store versioned feature definitions, computed feature snapshots,
and feature lineage for reproducibility.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from fpl_intelligence.db.base import Base


class FeatureDefinition(Base):
    """Immutable definition of a feature.

    Once a feature definition is used in a historical backtest, it must not be
    modified. Create a new version for changed logic.

    Attributes:
        feature_name: Canonical name, e.g. "player_form".
        description: Human-readable description.
        data_type: "float", "int", "json", etc.
        entity_type: "player", "team", "fixture".
        version: Semantic version string, e.g. "1.0.0".
        calculation_method: Description of how the feature is computed.
        created_at: When this definition was created.
        is_active: Whether this version is currently active.
    """

    __tablename__ = "feature_definitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    data_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    calculation_method: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    is_active: Mapped[bool] = mapped_column(default=True)

    __table_args__ = (UniqueConstraint("feature_name", "version", name="uq_feature_name_version"),)


class FeatureSnapshot(Base):
    """A computed feature value at a specific cutoff time.

    Attributes:
        entity_id: ID of the entity (player, team, fixture).
        feature_name: Name of the feature.
        feature_version: Version of the feature definition used.
        cutoff_time: The historical decision cutoff for this snapshot.
        value: The computed feature value (JSON-encoded).
        is_missing: Whether the feature value is missing (distinct from zero).
        completeness_score: 0.0 to 1.0 indicating data completeness.
        source_count: Number of source records used.
        latest_source_time: Timestamp of the most recent source record.
        created_at: When this snapshot was computed.
    """

    __tablename__ = "feature_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    feature_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    feature_version: Mapped[str] = mapped_column(String(20), nullable=False)
    cutoff_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_missing: Mapped[bool] = mapped_column(default=False)
    completeness_score: Mapped[float | None] = mapped_column(Float)
    source_count: Mapped[int | None] = mapped_column(Integer)
    latest_source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "entity_id",
            "feature_name",
            "feature_version",
            "cutoff_time",
            name="uq_feature_snapshot_entity_cutoff",
        ),
        Index(
            "ix_feature_snapshot_lookup",
            "feature_name",
            "feature_version",
            "cutoff_time",
        ),
    )


class FeatureLineage(Base):
    """Records the source data used to compute a feature.

    This enables reproducibility and auditing of feature calculations.

    Attributes:
        feature_name: Name of the feature.
        feature_version: Version of the feature definition.
        entity_id: ID of the entity.
        source_table: Name of the source table.
        source_record_ids: List of source record IDs used.
        calculation_version: Version of the calculation logic.
        cutoff_time: The cutoff time for this computation.
        created_at: When this lineage record was created.
    """

    __tablename__ = "feature_lineage"

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    feature_version: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_table: Mapped[str] = mapped_column(String(200), nullable=False)
    source_record_ids: Mapped[list[int] | None] = mapped_column(JSON)
    calculation_version: Mapped[str] = mapped_column(String(20), nullable=False)
    cutoff_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_feature_lineage_lookup", "feature_name", "entity_id", "cutoff_time"),
    )
