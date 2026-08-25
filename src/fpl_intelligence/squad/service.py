"""Squad state management service (Phase 11.2 — PostgreSQL-backed).

The service persists the user's squad state to PostgreSQL via SQLAlchemy while
remaining usable without a database (an in-memory fallback keeps unit tests and
local scripts offline-friendly). It is thread-safe: a process-local lock
serialises same-process access, and cross-process / multi-worker concurrency is
resolved by an idempotent upsert on the unique ``session_id`` key.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fpl_intelligence.squad.models import SquadStateCreate, SquadStateResponse
from fpl_intelligence.squad.models_db import LocalSquadStateDB, SquadStateDB

logger = logging.getLogger(__name__)

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
        self._local_state: SquadStateResponse | None = None

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

    @staticmethod
    def _ensure_local_table(db: Session) -> None:
        try:
            # Check via inspector; fallback to try create.
            from sqlalchemy import inspect as _insp
            from sqlalchemy import text as _text

            insp = _insp(db.get_bind())
            if insp.has_table("local_squad_state"):
                return
            db.execute(
                _text(
                    """
                    CREATE TABLE IF NOT EXISTS local_squad_state (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(255) NOT NULL UNIQUE,
                        squad_json JSON NOT NULL,
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL
                    )
                    """
                )
            )
            # SQLite uses different DDL — the Postgres SERIAL above fails on sqlite.
            # create_all is dialect-safe and idempotent, so use it as fallback.
            try:
                db.commit()
            except Exception:
                db.rollback()
                from fpl_intelligence.db.base import Base as _Base  # noqa: PLC0415

                _Base.metadata.create_all(
                    db.get_bind(), tables=[LocalSquadStateDB.__table__]
                )
                db.commit()
        except Exception as exc:  # noqa: BLE001 — best-effort
            try:
                db.rollback()
            except Exception:
                pass
            logger.debug("local_squad DDL ensure skipped: %s", exc)

    def _upsert_local(self, db: Session, session_id: str, state: SquadStateResponse) -> None:
        # Self-seal table on first use (prod DB predates 0021).
        try:
            self._ensure_local_table(db)
        except Exception:
            pass
        data = self._row_data(session_id, state)
        try:
            existing = db.execute(
                select(LocalSquadStateDB).where(LocalSquadStateDB.session_id == session_id)
            ).scalar_one_or_none()
        except Exception:
            db.rollback()
            self._ensure_local_table(db)
            existing = db.execute(
                select(LocalSquadStateDB).where(LocalSquadStateDB.session_id == session_id)
            ).scalar_one_or_none()
        if existing is None:
            try:
                db.add(LocalSquadStateDB(**data))
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(LocalSquadStateDB).where(LocalSquadStateDB.session_id == session_id)
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

    def set_squad(self, payload: SquadStateCreate, session_id: str) -> SquadStateResponse:
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
                # Phase 25 (T1): capture a roster snapshot so the transfer
                # ledger can fall back to snapshot-diff when official history
                # is blocked. Best-effort — must never break a squad save.
                try:
                    from fpl_intelligence.transfers.service import capture_snapshot

                    capture_snapshot(
                        db,
                        session_id,
                        list(payload.player_ids),
                        int(payload.gameweek),
                        float(payload.bank or 0.0),
                    )
                except Exception as exc:  # noqa: BLE001 — observability only
                    logger.warning("squad snapshot capture failed: %s", exc)
                    with contextlib.suppress(Exception):
                        db.rollback()
            finally:
                if own:
                    db.close()
        return state

    def get_squad(self, session_id: str) -> SquadStateResponse | None:
        """Return the *base* (FPL-truth) squad state, or None if not set.

        v2.7.3: this is the raw FPL import row. User-facing math should use
        :meth:`get_effective_squad` which prefers the Transfer Planner's
        ``local_squad`` override.
        """
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

    # -- v2.7.3-dual-state: local_squad overlay --------------------------------

    def set_local_squad(self, payload: SquadStateCreate, session_id: str) -> SquadStateResponse:
        """Persist a *local* squad override (Transfer Planner, no FPL fetch).

        Overwrites ``local_squad_state`` for ``session_id``. All user-facing
        math reads the effective squad (local preferred, base fallback).
        """
        state = SquadStateResponse(**payload.model_dump(), updated_at=datetime.now(UTC))
        if not self._is_persistent():
            with self._lock:
                self._local_state = state
            return state
        with self._lock:
            db, own = self._acquire()
            try:
                self._upsert_local(db, session_id, state)
                # Also keep base snapshot-friendly: capture transfer diff.
                try:
                    from fpl_intelligence.transfers.service import capture_snapshot

                    capture_snapshot(
                        db,
                        session_id,
                        list(payload.player_ids),
                        int(payload.gameweek),
                        float(payload.bank or 0.0),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("local squad snapshot capture failed: %s", exc)
                    with contextlib.suppress(Exception):
                        db.rollback()
            finally:
                if own:
                    db.close()
        return state

    def get_local_squad(self, session_id: str) -> SquadStateResponse | None:
        """Return the local override, or None if the user never saved one."""
        if not self._is_persistent():
            with self._lock:
                return self._local_state
        with self._lock:
            db, own = self._acquire()
            try:
                row = db.execute(
                    select(LocalSquadStateDB).where(LocalSquadStateDB.session_id == session_id)
                ).scalar_one_or_none()
                if row is None:
                    return None
                return SquadStateResponse.model_validate(row.squad_json)
            finally:
                if own:
                    db.close()

    def get_effective_squad(self, session_id: str) -> SquadStateResponse | None:
        """User-facing squad: local override preferred, base fallback.

        This is what Captaincy, Alpha, Horizon Planner, Trajectory and FOMO
        all read. The daily league/rival fetch stays auto-fetched and
        unaffected.
        """
        # Fast path: in-memory mode
        if not self._is_persistent():
            with self._lock:
                if self._local_state is not None:
                    return self._local_state
                return self._state
        with self._lock:
            db, own = self._acquire()
            try:
                local_row = db.execute(
                    select(LocalSquadStateDB).where(LocalSquadStateDB.session_id == session_id)
                ).scalar_one_or_none()
                if local_row is not None:
                    return SquadStateResponse.model_validate(local_row.squad_json)
                base_row = db.execute(
                    select(SquadStateDB).where(SquadStateDB.session_id == session_id)
                ).scalar_one_or_none()
                if base_row is None:
                    return None
                return SquadStateResponse.model_validate(base_row.squad_json)
            finally:
                if own:
                    db.close()

    def clear_local(self, session_id: str) -> None:
        """Remove the local override (test helper)."""
        if not self._is_persistent():
            with self._lock:
                self._local_state = None
            return
        with self._lock:
            db, own = self._acquire()
            try:
                row = db.execute(
                    select(LocalSquadStateDB).where(LocalSquadStateDB.session_id == session_id)
                ).scalar_one_or_none()
                if row is not None:
                    db.delete(row)
                    db.commit()
            finally:
                if own:
                    db.close()

    def clear(self, session_id: str) -> None:
        """Remove the stored squad state (test helper / admin action).

        Clears **both** base and local rows so tests start clean. Production
        "Start Over" should clear via the same call site.
        """
        if not self._is_persistent():
            with self._lock:
                self._state = None
                self._local_state = None
            return

        with self._lock:
            db, own = self._acquire()
            try:
                for model in (SquadStateDB, LocalSquadStateDB):
                    row = db.execute(
                        select(model).where(model.session_id == session_id)  # type: ignore[arg-type]
                    ).scalar_one_or_none()
                    if row is not None:
                        db.delete(row)
                db.commit()
            finally:
                if own:
                    db.close()
