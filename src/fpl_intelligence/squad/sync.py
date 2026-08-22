"""Phase 13.5 — pending squad auto-sync handling.

Persists a queued FPL squad import (``auto_sync=true``) whenever
``POST /api/v1/squad/from-fpl`` fails with a transient 503, then lets both the
existing daily ``run-scheduler`` cron and the public ``/squad/retry-sync``
endpoint retry it. On success the squad is saved and a Telegram push is sent
(no new secrets, no new cron slot, no GitHub Actions).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.notifications.sync_notifier import (
    send_squad_synced_notification,
)
from fpl_intelligence.squad.fpl_import import (
    FplImportError,
    FplImportResult,
    FplSquadImporter,
)
from fpl_intelligence.squad.models_db import PendingSyncDB
from fpl_intelligence.squad.service import SquadService

logger = logging.getLogger(__name__)

PENDING = "PENDING"
SYNCED = "SYNCED"
FAILED = "FAILED"


class NoPendingSync(Exception):
    """No auto-sync request has been queued."""


def save_pending_sync(db: Session, entry_id: int) -> PendingSyncDB:
    """Queue (or refresh) a pending auto-sync for ``entry_id`` with auto_sync=true."""
    now = datetime.now(UTC)
    row = db.scalar(select(PendingSyncDB).order_by(PendingSyncDB.id.desc()))
    if row is None:
        row = PendingSyncDB(
            entry_id=entry_id,
            auto_sync=True,
            status=PENDING,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.entry_id = entry_id
        row.auto_sync = True
        row.status = PENDING
        row.updated_at = now
    db.commit()
    return row


def get_pending_sync(db: Session) -> PendingSyncDB | None:
    """Return the most recent PENDING auto-sync row, or None."""
    return db.scalar(
        select(PendingSyncDB)
        .where(PendingSyncDB.status == PENDING)
        .order_by(PendingSyncDB.id.desc())
    )


def _mark(db: Session, row: PendingSyncDB, status: str) -> None:
    row.status = status
    row.updated_at = datetime.now(UTC)
    db.commit()


async def run_pending_sync(db: Session) -> FplImportResult:
    """Attempt the queued auto-sync: import → save squad → notify.

    Raises:
        NoPendingSync: nothing is queued.
        FplImportError: the upstream FPL API was unreachable / rejected the
            request (the queued row is marked FAILED).
    """
    row = get_pending_sync(db)
    if row is None:
        raise NoPendingSync("No squad sync is pending.")

    importer = FplSquadImporter()
    try:
        result = await importer.build_squad_from_entry(row.entry_id, db)
    except FplImportError:
        logger.warning("Auto-sync failed for entry %s; marking FAILED.", row.entry_id)
        _mark(db, row, FAILED)
        raise

    SquadService(session=db).set_squad(result.squad, session_id=str(row.entry_id))
    _mark(db, row, SYNCED)
    await send_squad_synced_notification(result.entry_name)
    return result
