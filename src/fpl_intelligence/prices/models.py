"""Phase 23 Gate 1 (L3) — price engine persistence models.

* ``price_moves``     — one row per detected daily now_cost change
                        (▲ risers / ▼ fallers chips + top-5 strips).
* ``price_snapshots`` — per-day now_cost history for every element so any
                        past move can be replayed from stored truth.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fpl_intelligence.db.base import Base


class PriceMoveDB(Base):
    """One element's price change for one gameweek (delta in £0.1m units)."""

    __tablename__ = "price_moves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    element_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    gameweek: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    old_cost: Mapped[float | None] = mapped_column(Float)
    new_cost: Mapped[float | None] = mapped_column(Float)
    delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    moved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("gameweek", "element_id", "delta", name="uq_price_move"),
    )


class PriceSnapshotDB(Base):
    """One element's now_cost on one calendar day (daily snapshot history)."""

    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    element_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    now_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("snapshot_date", "element_id", name="uq_price_snapshot"),
    )
