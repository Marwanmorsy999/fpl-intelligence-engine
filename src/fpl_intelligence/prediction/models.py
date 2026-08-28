"""SQLAlchemy models for the Phase 4 prediction layer.

These tables persist:

- ``model_registry``: registered model versions and their artifacts.
- ``model_predictions``: immutable per-entity predictions (players/teams/fixtures).
- ``team_strengths``: per-team, per-cutoff strength estimates.
- ``match_predictions``: per-fixture probabilistic match predictions.

All prediction records are immutable historical facts: never overwrite an
existing row; insert new rows with a new decision cutoff instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fpl_intelligence.db.models import Fixture, Team

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fpl_intelligence.db.base import Base


class ModelRegistryEntry(Base):
    """A registered model version with its artifact and metrics.

    Attributes:
        model_name: Canonical model name (e.g. ``minutes_model``).
        model_version: Semantic version (e.g. ``1.0.0``).
        model_type: e.g. ``classification`` / ``regression`` / ``baseline``.
        feature_version: Feature-store version used to train.
        training_cutoff: The decision cutoff used for training data.
        training_start: Start of the training window.
        training_end: End of the training window.
        hyperparameters: JSON dict of hyperparameters.
        random_seed: Seed used during training.
        training_sample_count: Number of rows used for training.
        metrics: JSON dict of evaluation metrics.
        artifact_location: Path/URI of the persisted artifact.
        status: ``staged``, ``active``, ``retired``, ``archived``.
        created_at: When the entry was created.
    """

    __tablename__ = "model_registry"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(20), nullable=False)
    model_type: Mapped[str | None] = mapped_column(String(50))
    feature_version: Mapped[str | None] = mapped_column(String(20))
    training_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    training_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    training_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hyperparameters: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    random_seed: Mapped[int | None] = mapped_column(Integer)
    training_sample_count: Mapped[int | None] = mapped_column(Integer)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    artifact_location: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="staged")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("model_name", "model_version", name="uq_model_registry_name_version"),
    )


class ModelPrediction(Base):
    """An immutable model prediction for a player, team, or fixture.

    Attributes:
        model_name: Model that produced the prediction.
        model_version: Version of the model.
        feature_version: Feature-store version used.
        cutoff_time: The decision cutoff.
        entity_type: ``player``, ``team``, or ``fixture``.
        entity_id: ID of the entity.
        prediction_value: The primary prediction value.
        prediction_lower: Lower bound estimate.
        prediction_upper: Upper bound estimate.
        prediction_data: JSON of additional prediction outputs.
        confidence: Optional confidence/calibration measure.
        data_completeness: Explainable 0-1 completeness score.
        prediction_timestamp: When the prediction was generated.
        is_frozen: Whether the prediction is frozen (always True post-commit).
    """

    __tablename__ = "model_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(20), nullable=False)
    feature_version: Mapped[str | None] = mapped_column(String(20))
    cutoff_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    prediction_value: Mapped[float | None] = mapped_column(Float)
    prediction_lower: Mapped[float | None] = mapped_column(Float)
    prediction_upper: Mapped[float | None] = mapped_column(Float)
    prediction_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Float)
    data_completeness: Mapped[float | None] = mapped_column(Float)
    prediction_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    is_frozen: Mapped[bool] = mapped_column(default=True)

    __table_args__ = (
        Index(
            "ix_model_predictions_lookup",
            "model_name",
            "model_version",
            "entity_type",
            "entity_id",
            "cutoff_time",
        ),
    )


class TeamStrengthRecord(Base):
    """Per-team strength estimates as of a cutoff.

    Attributes:
        team_id: Team ID.
        season: Season code.
        cutoff_time: The decision cutoff.
        feature_version: Feature-store version used.
        attack_strength: Offensive strength (interpretable scale).
        defence_strength: Defensive strength.
        home_strength: Strength when playing at home.
        away_strength: Strength when playing away.
        sample_size: Number of source matches used.
        completeness: Data-completeness score.
        created_at: When the estimate was created.
    """

    __tablename__ = "team_strengths"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)
    season: Mapped[str] = mapped_column(String(20), nullable=False)
    cutoff_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    feature_version: Mapped[str | None] = mapped_column(String(20))
    attack_strength: Mapped[float | None] = mapped_column(Float)
    defence_strength: Mapped[float | None] = mapped_column(Float)
    home_strength: Mapped[float | None] = mapped_column(Float)
    away_strength: Mapped[float | None] = mapped_column(Float)
    home_attack_strength: Mapped[float | None] = mapped_column(Float)
    away_attack_strength: Mapped[float | None] = mapped_column(Float)
    home_defence_strength: Mapped[float | None] = mapped_column(Float)
    away_defence_strength: Mapped[float | None] = mapped_column(Float)
    sample_size: Mapped[int | None] = mapped_column(Integer)
    completeness: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    team: Mapped[Team] = relationship("Team")

    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "season",
            "cutoff_time",
            "feature_version",
            name="uq_team_strength_cutoff",
        ),
    )


class MatchPredictionRecord(Base):
    """A probabilistic match prediction for a fixture.

    Attributes:
        fixture_id: Fixture ID.
        season: Season code.
        cutoff_time: The decision cutoff.
        feature_version: Feature-store version used.
        model_name: Model that produced the prediction.
        model_version: Version of the model.
        expected_home_goals: Expected home goals.
        expected_away_goals: Expected away goals.
        home_win_probability: P(home win).
        draw_probability: P(draw).
        away_win_probability: P(away win).
        home_clean_sheet_probability: P(home clean sheet).
        away_clean_sheet_probability: P(away clean sheet).
        scoreline_distribution: Optional JSON of simulated scorelines.
        simulation_count: Number of simulations (if applicable).
        random_seed: Seed used for simulation.
        created_at: When the prediction was created.
    """

    __tablename__ = "match_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), nullable=False, index=True)
    season: Mapped[str] = mapped_column(String(20), nullable=False)
    cutoff_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    feature_version: Mapped[str | None] = mapped_column(String(20))
    model_name: Mapped[str | None] = mapped_column(String(100))
    model_version: Mapped[str | None] = mapped_column(String(20))
    expected_home_goals: Mapped[float | None] = mapped_column(Float)
    expected_away_goals: Mapped[float | None] = mapped_column(Float)
    home_win_probability: Mapped[float | None] = mapped_column(Float)
    draw_probability: Mapped[float | None] = mapped_column(Float)
    away_win_probability: Mapped[float | None] = mapped_column(Float)
    home_clean_sheet_probability: Mapped[float | None] = mapped_column(Float)
    away_clean_sheet_probability: Mapped[float | None] = mapped_column(Float)
    scoreline_distribution: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    simulation_count: Mapped[int | None] = mapped_column(Integer)
    random_seed: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    fixture: Mapped[Fixture] = relationship("Fixture")

    __table_args__ = (
        UniqueConstraint(
            "fixture_id",
            "cutoff_time",
            "model_name",
            "model_version",
            name="uq_match_prediction_fixture_cutoff",
        ),
    )
