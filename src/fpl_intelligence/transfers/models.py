"""Phase 25 Gate 0 (T1) — persistence for the transfer ledger.

* ``transfer_log``      — one row per observed transfer (official history or
                          snapshot-diff), keyed by entry + gameweek.
* ``squad_snapshot``    — the 15 player ids captured at every squad save so
                          the snapshot-diff fallback can diff consecutive
                          states when the official history is unreachable.

Both tables are self-sealing at request time (see :mod:`transfers.service`)
so deployments predating migration 0021 keep working.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fpl_intelligence.db.base import Base


class TransferLogDB(Base):
    """One materialized transfer: element_in, element_out, cost."""

    __tablename__ = "transfer_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    gameweek: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    #: Official FPL transfer id (None for snapshot-diff rows).
    transfer_id: Mapped[int | None] = mapped_column(Integer)
    element_in: Mapped[int | None] = mapped_column(Integer)
    element_out: Mapped[int | None] = mapped_column(Integer)
    name_in: Mapped[str | None] = mapped_column(String(120))
    name_out: Mapped[str | None] = mapped_column(String(120))
    #: Points hit charged for this transfer (0 = free).
    cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: ``official-history`` or ``snapshot-diff (unofficial)``.
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="official-history")
    #: True once horizon EV has been computed and stored.
    ev_computed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    horizon_ev: Mapped[float | None] = mapped_column(Float)
    horizon_gws: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("entry_id", "transfer_id", name="uq_transfer_entry_tid"),
    )


class SquadSnapshotDB(Base):
    """The full 15-man squad captured whenever a squad state is saved."""

    __tablename__ = "squad_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    gameweek: Mapped[int] = mapped_column(Integer, nullable=False)
    player_ids: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    bank: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
