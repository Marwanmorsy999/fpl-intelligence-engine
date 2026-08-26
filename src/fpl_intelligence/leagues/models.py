"""Phase 23 Gate 1 (L1) — league-cache persistence models.

* ``entry_leagues``   — every classic league each tracked entry belongs to,
                        discovered by the daily job through
                        ``/api/entry/{id}/leagues/`` (zero config, no
                        hardcoded league ids).
* ``league_cache``    — standings page 1 + capped rival picks per league,
                        refreshed daily and on demand (10-min cooldown).
* ``league_selection``— the user's remembered league pick when an entry
                        belongs to more than one classic league.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fpl_intelligence.db.base import Base


class EntryLeagueDB(Base):
    """One classic league one entry belongs to."""

    __tablename__ = "entry_leagues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    league_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    league_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    member_count: Mapped[int | None] = mapped_column(Integer)
    entry_rank: Mapped[int | None] = mapped_column(Integer)
    entry_last_rank: Mapped[int | None] = mapped_column(Integer)
    private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("entry_id", "league_id", name="uq_entry_league"),
    )


class LeagueCacheDB(Base):
    """Cached standings page 1 plus capped rival picks for one league."""

    __tablename__ = "league_cache"

    league_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    member_count: Mapped[int | None] = mapped_column(Integer)
    standings: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    #: {"gameweek": n, "cap": 10, "picks": {entry_id: [element ids]}, "partial": bool}
    rivals_picks: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LeagueSelectionDB(Base):
    """Remembered /league picker choice per session (= entry id)."""

    __tablename__ = "league_selection"

    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    league_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chosen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
