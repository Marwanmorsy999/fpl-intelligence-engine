"""Phase 23 — decision snapshot store (issue #23).

Stores a precomputed :class:`~fpl_intelligence.squad.models.DecisionReport`
so that the user request path can serve ``/api/v1/decisions`` from a
durable artifact instead of running the full Phase 6 decision chain on
every request.

Design rules
------------

* One canonical table ``decision_snapshots`` with a unique constraint
  on ``(session_id, gameweek, model_version, data_snapshot_id)`` so the
  same logical decision never collides with itself.
* Snapshots carry an explicit :class:`RefreshState` (``fresh``,
  ``refreshing``, ``stale``, ``unavailable``). The request path can
  return the current snapshot with its state, instead of blocking on
  recompute or claiming success when none exists.
* A Postgres advisory lock (``pg_try_advisory_lock``) guards concurrent
  rebuilds for the same ``(session_id, gameweek)``. Workers that lose
  the race surface the existing in-flight snapshot rather than
  duplicate work.
* The store is process-local when no database session is supplied
  (tests), so unit tests run without Postgres.

Out of scope (follow-ups):

* Wiring ``/api/v1/decisions`` to read from this store. That change
  touches route-handler contracts and is the next phase.
* The GitHub Actions schedule itself — the cron lives in
  ``.github/workflows/`` (already there as ``model-validation.yml``).
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RefreshState
# ---------------------------------------------------------------------------


class RefreshState(enum.StrEnum):
    """Lifecycle state of a decision snapshot.

    - ``fresh``        — payload present, within refresh window
    - ``refreshing``   — a background rebuild is in flight
    - ``stale``        — payload present, older than refresh window
    - ``unavailable``  — no payload could be produced (e.g. upstream
      failure with no prior snapshot)
    """

    FRESH = "fresh"
    REFRESHING = "refreshing"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SnapshotStoreError(RuntimeError):
    """Raised when the snapshot store cannot serve the request."""


class ConcurrentRebuildError(SnapshotStoreError):
    """Raised when a worker fails to acquire the per-snapshot rebuild lock.

    The caller should surface the existing snapshot or fall back to a
    blocking rebuild.
    """


# ---------------------------------------------------------------------------
# Snapshot record
# ---------------------------------------------------------------------------


@dataclass
class DecisionSnapshot:
    """In-memory representation of one persisted decision snapshot."""

    session_id: str
    gameweek: int
    model_version: str
    data_snapshot_id: str
    payload: dict[str, Any]
    refresh_state: RefreshState
    generated_at: datetime
    refresh_window_seconds: float = 900.0  # 15 minutes default
    id: int | None = None

    @property
    def is_within_refresh_window(self) -> bool:
        age = (datetime.now(tz=UTC) - self.generated_at).total_seconds()
        return age <= self.refresh_window_seconds

    @property
    def age_seconds(self) -> float:
        return (datetime.now(tz=UTC) - self.generated_at).total_seconds()

    def effective_state(self) -> RefreshState:
        """Compute the *effective* state for a read.

        ``fresh`` snapshots whose age exceeds the refresh window are
        surfaced as ``stale`` so callers can trigger a refresh without
        blocking.
        """
        if self.refresh_state is RefreshState.FRESH and not self.is_within_refresh_window:
            return RefreshState.STALE
        return self.refresh_state


# ---------------------------------------------------------------------------
# Store protocols
# ---------------------------------------------------------------------------


class _Backend(Protocol):
    """Minimal interface the store needs from a database session.

    Concrete implementations:
    - :class:`InMemoryBackend` — used in tests
    - :class:`PostgresBackend` — used in production (uses advisory locks)
    """

    def fetch_latest(self, session_id: str, gameweek: int) -> DecisionSnapshot | None: ...

    def fetch_for_refresh_state(
        self, session_id: str, gameweek: int, model_version: str, data_snapshot_id: str
    ) -> DecisionSnapshot | None: ...

    def try_mark_refreshing(
        self, session_id: str, gameweek: int, model_version: str, data_snapshot_id: str
    ) -> bool:
        """Atomically transition ``missing/stale`` -> ``refreshing``.

        Returns ``True`` if the caller now owns the rebuild slot,
        ``False`` if another worker is already refreshing.
        """
        ...

    def upsert(
        self,
        snapshot: DecisionSnapshot,
    ) -> DecisionSnapshot: ...

    def mark_state(self, snapshot_id: int, new_state: RefreshState) -> None: ...


# ---------------------------------------------------------------------------
# In-memory backend (tests)
# ---------------------------------------------------------------------------


class InMemoryBackend:
    def __init__(self) -> None:
        self._by_session: dict[tuple[str, int], list[DecisionSnapshot]] = {}
        # Rebuild locks: (session_id, gameweek) -> True if held.
        # The store-level slot already carries (model_version, data_snapshot_id)
        # so the lock just needs to be free-or-held per key.
        self._locks: dict[tuple[str, int], bool] = {}
        self._lock = __import__("threading").Lock()

    def fetch_latest(self, session_id: str, gameweek: int) -> DecisionSnapshot | None:
        with self._lock:
            snaps = self._by_session.get((session_id, gameweek), [])
            if not snaps:
                return None
            return max(snaps, key=lambda s: s.generated_at)

    def fetch_for_refresh_state(
        self, session_id: str, gameweek: int, model_version: str, data_snapshot_id: str
    ) -> DecisionSnapshot | None:
        with self._lock:
            for snap in self._by_session.get((session_id, gameweek), []):
                if (
                    snap.model_version == model_version
                    and snap.data_snapshot_id == data_snapshot_id
                ):
                    return snap
            return None

    def try_mark_refreshing(
        self, session_id: str, gameweek: int, model_version: str, data_snapshot_id: str
    ) -> bool:
        key = (session_id, gameweek)
        with self._lock:
            if self._locks.get(key):
                return False
            self._locks[key] = True
            return True

    def release_lock(
        self, session_id: str, gameweek: int, model_version: str, data_snapshot_id: str
    ) -> None:
        key = (session_id, gameweek)
        with self._lock:
            self._locks.pop(key, None)

    def upsert(self, snapshot: DecisionSnapshot) -> DecisionSnapshot:
        with self._lock:
            existing = self._by_session.get((snapshot.session_id, snapshot.gameweek), [])
            snapshot.id = len(existing) + 1
            self._by_session.setdefault((snapshot.session_id, snapshot.gameweek), []).append(
                snapshot
            )
            return snapshot

    def mark_state(self, snapshot_id: int, new_state: RefreshState) -> None:
        with self._lock:
            for snaps in self._by_session.values():
                for snap in snaps:
                    if snap.id == snapshot_id:
                        snap.refresh_state = new_state
                        return


# ---------------------------------------------------------------------------
# Postgres backend (production)
# ---------------------------------------------------------------------------


class PostgresBackend:
    """Postgres-backed snapshot store.

    Uses ``pg_try_advisory_lock`` for per-(session_id, gameweek)
    rebuild guards and ``UPSERT`` (Postgres-specific ``ON CONFLICT``)
    for atomic writes.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    def fetch_latest(self, session_id: str, gameweek: int) -> DecisionSnapshot | None:
        from sqlalchemy import select

        from fpl_intelligence.sync.materialized_models import DecisionSnapshotDB

        row = self._session.execute(
            select(DecisionSnapshotDB)
            .where(
                DecisionSnapshotDB.session_id == str(session_id),
                DecisionSnapshotDB.gameweek == int(gameweek),
            )
            .order_by(DecisionSnapshotDB.generated_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return _row_to_snapshot(row)

    def fetch_for_refresh_state(
        self, session_id: str, gameweek: int, model_version: str, data_snapshot_id: str
    ) -> DecisionSnapshot | None:
        from sqlalchemy import select

        from fpl_intelligence.sync.materialized_models import DecisionSnapshotDB

        row = self._session.execute(
            select(DecisionSnapshotDB).where(
                DecisionSnapshotDB.session_id == str(session_id),
                DecisionSnapshotDB.gameweek == int(gameweek),
                DecisionSnapshotDB.model_version == str(model_version),
                DecisionSnapshotDB.data_snapshot_id == str(data_snapshot_id),
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _row_to_snapshot(row)

    def try_mark_refreshing(
        self, session_id: str, gameweek: int, model_version: str, data_snapshot_id: str
    ) -> bool:
        from sqlalchemy import text

        # 64-bit advisory lock: top half = hash(session_id), bottom half = gameweek.
        lock_key1 = _lock_key_part(session_id)
        lock_key2 = int(gameweek)
        result = self._session.execute(
            text("SELECT pg_try_advisory_lock(:k1, :k2)"),
            {"k1": lock_key1, "k2": lock_key2},
        ).scalar()
        return bool(result)

    def release_lock(self, session_id: str, gameweek: int) -> None:
        from sqlalchemy import text

        self._session.execute(
            text("SELECT pg_advisory_unlock(:k1, :k2)"),
            {"k1": _lock_key_part(session_id), "k2": int(gameweek)},
        )

    def upsert(self, snapshot: DecisionSnapshot) -> DecisionSnapshot:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from fpl_intelligence.sync.materialized_models import DecisionSnapshotDB

        stmt = pg_insert(DecisionSnapshotDB).values(
            session_id=str(snapshot.session_id),
            gameweek=int(snapshot.gameweek),
            model_version=str(snapshot.model_version),
            data_snapshot_id=str(snapshot.data_snapshot_id),
            payload=json.loads(json.dumps(snapshot.payload)),  # ensure JSON-safe
            refresh_state=snapshot.refresh_state.value,
            generated_at=snapshot.generated_at,
            refresh_window_seconds=float(snapshot.refresh_window_seconds),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                DecisionSnapshotDB.session_id,
                DecisionSnapshotDB.gameweek,
                DecisionSnapshotDB.model_version,
                DecisionSnapshotDB.data_snapshot_id,
            ],
            set_={
                "payload": stmt.excluded.payload,
                "refresh_state": stmt.excluded.refresh_state,
                "generated_at": stmt.excluded.generated_at,
                "refresh_window_seconds": stmt.excluded.refresh_window_seconds,
            },
        )
        self._session.execute(stmt)
        self._session.commit()
        # Read it back to obtain the id
        row = self._session.execute(
            select(DecisionSnapshotDB).where(
                DecisionSnapshotDB.session_id == snapshot.session_id,
                DecisionSnapshotDB.gameweek == snapshot.gameweek,
                DecisionSnapshotDB.model_version == snapshot.model_version,
                DecisionSnapshotDB.data_snapshot_id == snapshot.data_snapshot_id,
            )
        ).scalar_one()
        return _row_to_snapshot(row)

    def mark_state(self, snapshot_id: int, new_state: RefreshState) -> None:
        from sqlalchemy import update

        from fpl_intelligence.sync.materialized_models import DecisionSnapshotDB

        self._session.execute(
            update(DecisionSnapshotDB)
            .where(DecisionSnapshotDB.id == int(snapshot_id))
            .values(refresh_state=new_state.value)
        )
        self._session.commit()


def _lock_key_part(session_id: str) -> int:
    """Stable 32-bit hash for a session id (advisory lock first arg)."""
    import hashlib

    h = hashlib.sha1(session_id.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big", signed=True)


def _row_to_snapshot(row: Any) -> DecisionSnapshot:
    state = RefreshState(row.refresh_state) if row.refresh_state else RefreshState.FRESH
    return DecisionSnapshot(
        id=row.id,
        session_id=row.session_id,
        gameweek=row.gameweek,
        model_version=row.model_version,
        data_snapshot_id=row.data_snapshot_id,
        payload=row.payload or {},
        refresh_state=state,
        generated_at=row.generated_at,
        refresh_window_seconds=float(getattr(row, "refresh_window_seconds", 900.0) or 900.0),
    )


# ---------------------------------------------------------------------------
# Public store API
# ---------------------------------------------------------------------------


@dataclass
class DecisionSnapshotStore:
    """Read-through + stateful-write snapshot store."""

    backend: _Backend
    default_refresh_window_seconds: float = 900.0

    def get_latest(
        self, session_id: str, gameweek: int
    ) -> tuple[RefreshState, DecisionSnapshot | None]:
        """Return the latest snapshot's *effective* state and payload."""
        snap = self.backend.fetch_latest(session_id, gameweek)
        if snap is None:
            return RefreshState.UNAVAILABLE, None
        return snap.effective_state(), snap

    def acquire_rebuild_slot(
        self,
        session_id: str,
        gameweek: int,
        *,
        model_version: str,
        data_snapshot_id: str,
    ) -> RebuildSlot | None:
        """Try to claim the per-snapshot rebuild slot.

        Returns ``None`` if another worker is already refreshing.
        The returned :class:`RebuildSlot` context manager must be
        entered (or :meth:`release` called) when the rebuild finishes.
        """
        got = self.backend.try_mark_refreshing(
            session_id, gameweek, model_version, data_snapshot_id
        )
        if not got:
            return None
        return RebuildSlot(
            store=self,
            session_id=session_id,
            gameweek=gameweek,
            model_version=model_version,
            data_snapshot_id=data_snapshot_id,
        )

    def store(
        self,
        session_id: str,
        gameweek: int,
        *,
        model_version: str,
        data_snapshot_id: str,
        payload: dict[str, Any],
        refresh_window_seconds: float | None = None,
    ) -> DecisionSnapshot:
        snap = DecisionSnapshot(
            session_id=session_id,
            gameweek=gameweek,
            model_version=model_version,
            data_snapshot_id=data_snapshot_id,
            payload=payload,
            refresh_state=RefreshState.FRESH,
            generated_at=datetime.now(tz=UTC),
            refresh_window_seconds=(
                float(refresh_window_seconds)
                if refresh_window_seconds is not None
                else self.default_refresh_window_seconds
            ),
        )
        return self.backend.upsert(snap)

    def mark_state(self, snapshot_id: int, new_state: RefreshState) -> None:
        self.backend.mark_state(snapshot_id, new_state)


@dataclass
class RebuildSlot:
    """Context manager around a per-snapshot rebuild lock.

    Usage::

        slot = store.acquire_rebuild_slot(...)
        if slot is None:
            return existing_snapshot  # someone else is rebuilding
        with slot:
            payload = build_decision_report(...)
            store.store(...)
    """

    store: DecisionSnapshotStore
    session_id: str
    gameweek: int
    model_version: str
    data_snapshot_id: str
    _released: bool = field(default=False, init=False)

    def __enter__(self) -> RebuildSlot:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        release = getattr(self.store.backend, "release_lock", None)
        if release is not None:
            try:
                release(self.session_id, self.gameweek, self.model_version, self.data_snapshot_id)
            except Exception:  # noqa: BLE001 — best-effort
                logger.debug("release_lock failed for %s gw=%s", self.session_id, self.gameweek)


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def in_memory_store(refresh_window_seconds: float = 900.0) -> DecisionSnapshotStore:
    """Build a fully in-memory snapshot store for tests and dev."""
    return DecisionSnapshotStore(
        backend=InMemoryBackend(),
        default_refresh_window_seconds=refresh_window_seconds,
    )
