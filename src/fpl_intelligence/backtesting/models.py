"""Data models for the backtesting engine.

These models store backtest configurations, runs, gameweek results,
and individual player predictions. They are separate from the feature
store models and are used to track backtest execution and results.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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


class BacktestConfig(Base):
    """Configuration for a backtest run.

    Attributes:
        season: Season code (e.g., "2025-26").
        start_gameweek: First gameweek to backtest.
        end_gameweek: Last gameweek to backtest.
        decision_timing: When decisions are made ("deadline", "kickoff", etc.).
        information_access_policy: The temporal policy to enforce.
        feature_version: Version of features used.
        model_version: Version of the prediction model.
        random_seed: Seed for reproducible randomness.
        simulation_count: Number of Monte Carlo simulations.
        config_data: Additional configuration as JSON.
    """
    __tablename__ = "backtest_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    season: Mapped[str] = mapped_column(String(20), nullable=False)
    start_gameweek: Mapped[int] = mapped_column(Integer, nullable=False)
    end_gameweek: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_timing: Mapped[str] = mapped_column(String(50), default="deadline")
    information_access_policy: Mapped[str] = mapped_column(
        String(50), default="strict_reproducibility"
    )
    feature_version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    model_version: Mapped[str] = mapped_column(String(20), default="baseline")
    random_seed: Mapped[int | None] = mapped_column(Integer)
    simulation_count: Mapped[int] = mapped_column(Integer, default=1)
    config_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )


class BacktestRun(Base):
    """A single execution of a backtest.

    Attributes:
        run_id: Unique identifier for this run.
        config_id: Foreign key to the configuration.
        created_at: When the run was started.
        status: "running", "completed", "failed".
        feature_version: Version of features used in this run.
        model_version: Version of the model used in this run.
        error_summary: Error message if the run failed.
    """
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    config_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_configs.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    status: Mapped[str] = mapped_column(String(30), default="running")
    feature_version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    model_version: Mapped[str] = mapped_column(String(20), default="baseline")
    error_summary: Mapped[str | None] = mapped_column(Text)

    config: Mapped[BacktestConfig] = relationship()
    gameweek_results: Mapped[list[BacktestGameweekResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    predictions: Mapped[list[PlayerPrediction]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class BacktestGameweekResult(Base):
    """Results for a single gameweek within a backtest run.

    Attributes:
        run_id: Foreign key to the backtest run.
        season: Season code.
        gameweek: Gameweek number.
        decision_cutoff: The cutoff time used for decisions.
        predictions: JSON of all predictions for this gameweek.
        actual_outcomes: JSON of actual outcomes (for evaluation only).
        evaluation_metrics: JSON of evaluation metrics.
    """
    __tablename__ = "backtest_gameweek_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id"), nullable=False
    )
    season: Mapped[str] = mapped_column(String(20), nullable=False)
    gameweek: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    predictions: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    actual_outcomes: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    evaluation_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    run: Mapped[BacktestRun] = relationship(back_populates="gameweek_results")

    __table_args__ = (
        Index("ix_backtest_gw_results_run_id", "run_id"),
        Index(
            "ix_backtest_gw_results_run_gw",
            "run_id",
            "season",
            "gameweek",
        ),
    )


class PlayerPrediction(Base):
    """A single player prediction within a backtest run.

    Attributes:
        run_id: Foreign key to the backtest run.
        player_id: Foreign key to the player.
        fixture_id: Foreign key to the fixture (optional).
        cutoff: The decision cutoff time.
        predicted_expected_points: The model's prediction.
        prediction_interval_lower: Lower bound of prediction interval.
        prediction_interval_upper: Upper bound of prediction interval.
        feature_version: Version of features used.
        model_version: Version of the model used.
        confidence: Model confidence score (0-1).
        data_completeness: Completeness of input data (0-1).
        is_frozen: Whether this prediction is frozen (immutable).
    """
    __tablename__ = "player_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id"), nullable=False
    )
    fixture_id: Mapped[int | None] = mapped_column(
        ForeignKey("fixtures.id")
    )
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    predicted_expected_points: Mapped[float | None] = mapped_column(Float)
    prediction_interval_lower: Mapped[float | None] = mapped_column(Float)
    prediction_interval_upper: Mapped[float | None] = mapped_column(Float)
    feature_version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    model_version: Mapped[str] = mapped_column(String(20), default="baseline")
    confidence: Mapped[float | None] = mapped_column(Float)
    data_completeness: Mapped[float | None] = mapped_column(Float)
    is_frozen: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    run: Mapped[BacktestRun] = relationship(back_populates="predictions")

    __table_args__ = (
        Index("ix_player_predictions_run_id", "run_id"),
        Index("ix_player_predictions_player_id", "player_id"),
        Index("ix_player_predictions_fixture_id", "fixture_id"),
        Index(
            "ix_player_predictions_run_player",
            "run_id",
            "player_id",
        ),
        UniqueConstraint(
            "run_id", "player_id", "fixture_id", "cutoff",
            name="uq_prediction_run_player_fixture_cutoff",
        ),
    )


def create_backtest_run_id() -> str:
    """Generate a unique run ID."""
    return str(uuid.uuid4())
