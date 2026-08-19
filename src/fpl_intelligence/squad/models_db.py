"""SQLAlchemy persistence model for the user's squad state (Phase 11.2).

This model backs :class:`~fpl_intelligence.squad.service.SquadService` so the
personalised squad survives application restarts and is shared across multiple
workers / processes. The full :class:`SquadStateResponse` payload is stored as
JSON against a unique ``session_id`` key.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fpl_intelligence.db.base import Base


class SquadStateDB(Base):
    """Persisted squad state row."""

    __tablename__ = "squad_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    squad_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", name="uq_squad_state_session"),
    )
