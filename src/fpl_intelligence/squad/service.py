"""Squad state management service (Phase 11.2 — PostgreSQL-backed).

The service persists the user's squad state to PostgreSQL via SQLAlchemy while
remaining usable without a database (an in-memory fallback keeps unit tests and
local scripts offline-friendly). It is thread-safe: a process-local lock
serialises same-process access, and cross-process / multi-worker concurrency is
resolved by an idempotent upsert on the unique ``session_id`` key.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fpl_intelligence.squad.models import SquadStateCreate, SquadStateResponse
from fpl_intelligence.squad.models_db import SquadStateDB

#: A callable that returns a fresh SQLAlchemy ``Session``.
SessionFactory = Callable[[], Session]


class SquadService:
    """Persist and retrieve the user's squad state.

    Three operating modes, chosen by what is supplied at construction:

    * ``session`` — bind to an already-open request-scoped ``Session`` (the API
      route uses this so the squad lives in the request's transaction).
    * ``session_factory`` — open and close a ``Session`` per operation (the
      production worker / CLI path; also exercises the upsert + concurrency
      handling).
    * neither — keep an in-memory copy (offline mode; unit tests).
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        session: Session | None = None,
    ) -> None:
        if session_factory is not None and session is not None:
            raise ValueError("pass at most one of session_factory / session")
        self._session_factory = session_factory
        self._session = session
        self._lock = Lock()
        self._state: SquadStateResponse | None = None

    # -- internal helpers ---------------------------------------------------

    def _is_persistent(self) -> bool:
        return self._session is not None or self._session_factory is not None

    def _acquire(self) -> tuple[Session, bool]:
        if self._session is not None:
            return self._session, False
        assert self._session_factory is not None
        return self._session_factory(), True

    @staticmethod
    def _row_data(session_id: str, state: SquadStateResponse) -> dict:
        return {
            "session_id": session_id,
            "squad_json": state.model_dump(mode="json"),
            "updated_at": state.updated_at or datetime.utcnow(),
        }

    def _upsert(self, db: Session, session_id: str, state: SquadStateResponse) -> None:
        data = self._row_data(session_id, state)
        existing = db.execute(
            select(SquadStateDB).where(SquadStateDB.session_id == session_id)
        ).scalar_one_or_none()
        if existing is None:
            try:
                db.add(SquadStateDB(**data))
                db.commit()
            except IntegrityError:
                # Another process/worker inserted the row concurrently.
                db.rollback()
                existing = db.execute(
                    select(SquadStateDB).where(SquadStateDB.session_id == session_id)
                ).scalar_one_or_none()
                if existing is None:
                    raise
                existing.squad_json = data["squad_json"]
                existing.updated_at = data["updated_at"]
                db.commit()
        else:
            existing.squad_json = data["squad_json"]
            existing.updated_at = data["updated_at"]
            db.commit()

    # -- public API ---------------------------------------------------------

    def set_squad(
        self, payload: SquadStateCreate, session_id: str
    ) -> SquadStateResponse:
        """Persist a new squad state and return the stored representation."""
        state = SquadStateResponse(**payload.model_dump(), updated_at=datetime.now(UTC))
        if not self._is_persistent():
            with self._lock:
                self._state = state
            return state

        with self._lock:
            db, own = self._acquire()
            try:
                self._upsert(db, session_id, state)
            finally:
                if own:
                    db.close()
        return state

    def get_squad(self, session_id: str) -> SquadStateResponse | None:
        """Return the current squad state, or None if not set."""
        if not self._is_persistent():
            with self._lock:
                return self._state

        with self._lock:
            db, own = self._acquire()
            try:
                row = db.execute(
                    select(SquadStateDB).where(SquadStateDB.session_id == session_id)
                ).scalar_one_or_none()
                if row is None:
                    return None
                return SquadStateResponse.model_validate(row.squad_json)
            finally:
                if own:
                    db.close()

    def clear(self, session_id: str) -> None:
        """Remove the stored squad state (test helper / admin action)."""
        if not self._is_persistent():
            with self._lock:
                self._state = None
            return

        with self._lock:
            db, own = self._acquire()
            try:
                row = db.execute(
                    select(SquadStateDB).where(SquadStateDB.session_id == session_id)
                ).scalar_one_or_none()
                if row is not None:
                    db.delete(row)
                    db.commit()
            finally:
                if own:
                    db.close()
