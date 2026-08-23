"""Phase 19.0 — persistence models for the sync trio and derived math.

Five narrow tables; none of them touches Phase 1–18 tables:

* ``sync_live_points``   — per-player live points for a matchday gameweek
                           (pushed by the Apps Script matchday trigger).
* ``ingested_history``   — vaastav-format per-element gameweek results pushed
                           by GitHub Actions (source of truth for actuals).
* ``recommendation``     — every saved recommendation the engine made, keyed by
                           entry + gameweek + kind, auto-scored once the GW's
                           real results are ingested.
* ``prediction_ledger``  — predicted vs actual points per player/gameweek;
                           feeds the calibration readout.
* ``sync_log``           — audit trail powering /api/v1/sync/status and the
                           Sources page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fpl_intelligence.db.base import Base


class SyncLivePointDB(Base):
    """Live FPL points for one element in one matchday gameweek."""

    __tablename__ = "sync_live_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gameweek: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    element_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minutes: Mapped[int | None] = mapped_column(Integer)
    fixture_text: Mapped[str | None] = mapped_column(String(120))
    opponent: Mapped[str | None] = mapped_column(String(60))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("gameweek", "element_id", name="uq_sync_live_gw_element"),
    )


class IngestedGameweekDB(Base):
    """One element's finalised result for one gameweek (vaastav / Understat)."""

    __tablename__ = "ingested_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gameweek: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    element_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(60), nullable=False, default="github-actions")
    total_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minutes: Mapped[int | None] = mapped_column(Integer)
    bonus: Mapped[int | None] = mapped_column(Integer)
    goals_scored: Mapped[int | None] = mapped_column(Integer)
    assists: Mapped[int | None] = mapped_column(Integer)
    xgi: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("gameweek", "element_id", name="uq_ingested_gw_element"),
    )


class RecommendationDB(Base):
    """A saved engine recommendation for an entry, scored after the fact."""

    __tablename__ = "recommendation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    gameweek: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    rec_type: Mapped[str] = mapped_column(String(30), nullable=False)  # captain|transfer|xi|chip
    subject: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class PredictionLedgerDB(Base):
    """Predicted xPTS vs actual points per element/gameweek (calibration)."""

    __tablename__ = "prediction_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gameweek: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    element_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    predicted: Mapped[float] = mapped_column(Float, nullable=False)
    actual: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(60), nullable=False, default="baseline-model")
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("gameweek", "element_id", name="uq_pred_ledger_gw_element"),
    )


class SyncLogDB(Base):
    """Audit trail of every push/sync event (drives /sync/status)."""

    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # squad|live|history
    entry_id: Mapped[str | None] = mapped_column(String(255))
    gameweek: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
