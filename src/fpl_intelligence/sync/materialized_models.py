"""Phase 20.1 — materialized read models for the production incident.

Four narrow tables backing the "materialize, don't compute per request"
architecture. Every hot request path (decisions, drawer, brief, fixtures/scan,
news radar) reads ONLY these tables; the daily 06:10 cron is the sole writer.

* ``fixtures_cache``     — raw official fixtures array (JSON), fetched from
                           vaastav raw.githubusercontent (never blocked).
* ``news_cache``         — BBC Sport RSS items (JSON list), fetched directly.
* ``element_facts``      — per-element snapshot facts (minutes, selected-by %,
                           price change, status) from vaastav players_raw.csv.
* ``predictions_current``— precomputed per-player xPTS for the next 5 GWs,
                           produced once per day by the prediction chain.

None of these touch Phase 1-19 tables.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fpl_intelligence.db.base import Base


class FixturesCacheDB(Base):
    """Cached raw fixtures payload (single logical row, newest wins)."""

    __tablename__ = "fixtures_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(120), nullable=False, default="vaastav")
    payload: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NewsCacheDB(Base):
    """Cached BBC Sport football headlines (single logical row)."""

    __tablename__ = "news_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(120), nullable=False, default="bbc-rss")
    headline_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ElementFactDB(Base):
    """Bootstrap-style facts for one element, snapshotted at materialize time."""

    __tablename__ = "element_facts"

    element_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    web_name: Mapped[str | None] = mapped_column(String(120))
    team_id: Mapped[int | None] = mapped_column(Integer)
    minutes: Mapped[int | None] = mapped_column(Integer)
    selected_by_percent: Mapped[str | None] = mapped_column(String(20))
    cost_change_event: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(20))
    news: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PredictionCurrentDB(Base):
    """Precomputed xPTS row for one element in one gameweek."""

    __tablename__ = "predictions_current"

    gameweek: Mapped[int] = mapped_column(Integer, primary_key=True)
    element_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    expected_points: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    minutes_estimate: Mapped[float | None] = mapped_column(Float)
    start_prob: Mapped[float | None] = mapped_column(Float)
    xg_per_90: Mapped[float | None] = mapped_column(Float)
    xa_per_90: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(String(60))
    data_quality: Mapped[str | None] = mapped_column(String(60))
    breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("gameweek", "element_id", name="uq_pred_current_gw_element"),
    )


class LiveSnapshotDB(Base):
    """Phase 20.4 — last good live-matchday snapshot for one gameweek.

    Written whenever the live engine assembles a full payload; read back when
    every egress mask fails so the /live page shows honest stale data (with an
    age) instead of a blank screen.
    """

    __tablename__ = "live_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gameweek: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
