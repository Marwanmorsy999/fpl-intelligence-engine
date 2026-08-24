"""Phase 23 Gate 1 (L2) — Web Push endpoints + in-app bell read models.

Self-hosted: the VAPID keypair lives in env (no external accounts), browser
subscriptions persist in ``push_subscriptions`` and EVERY notification is
mirrored into ``notifications_log`` so the in-app bell keeps working even
when browser permission was denied. Telegram remains a parallel channel.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from fpl_intelligence.api import deps
from fpl_intelligence.notifications.webpush import (
    TRIGGERS,
    NotificationLogDB,
    PushSubscriptionDB,
    dispatch,
    ensure_push_tables,
    unread_count,
    vapid_configured,
    vapid_public_key,
)

router = APIRouter(prefix="/push", tags=["push"])
logger = logging.getLogger(__name__)

GetDB = deps.GetDB


class KeysBody(BaseModel):
    p256dh: str = Field(..., min_length=10)
    auth: str = Field(..., min_length=5)


class SubscribeBody(BaseModel):
    session_id: str = Field(..., min_length=1)
    endpoint: str = Field(..., min_length=10)
    keys: KeysBody
    triggers: dict[str, bool] = Field(default_factory=dict)


class UnsubscribeBody(BaseModel):
    endpoint: str = Field(..., min_length=10)


def _clean_triggers(raw: dict[str, bool]) -> dict[str, bool]:
    return {k: bool(v) for k, v in (raw or {}).items() if k in TRIGGERS}


@router.get("/config")
async def push_config() -> dict[str, Any]:
    """Public key + honest availability for the /connect enable button."""
    return {
        "vapid_public_key": vapid_public_key() or None,
        "configured": vapid_configured(),
        "triggers": list(TRIGGERS),
        "note": None if vapid_configured()
        else "VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY not set — bell still works, "
             "web push needs the keypair.",
    }


@router.post("/subscribe")
async def subscribe(body: SubscribeBody, db: GetDB) -> dict[str, Any]:
    """Upsert one subscription (endpoint-unique) with per-trigger toggles."""
    ensure_push_tables(db)
    row = db.scalar(
        select(PushSubscriptionDB).where(
            PushSubscriptionDB.endpoint == body.endpoint.strip()
        )
    )
    now = datetime.now(UTC)
    if row is None:
        row = PushSubscriptionDB(created_at=now)
        db.add(row)
    row.session_id = body.session_id
    row.endpoint = body.endpoint.strip()
    row.p256dh = body.keys.p256dh
    row.auth = body.keys.auth
    row.triggers = _clean_triggers(body.triggers)
    row.active = True
    db.commit()
    return {"ok": True, "triggers": row.triggers}


@router.post("/unsubscribe")
async def unsubscribe(body: UnsubscribeBody, db: GetDB) -> dict[str, Any]:
    ensure_push_tables(db)
    row = db.scalar(
        select(PushSubscriptionDB).where(
            PushSubscriptionDB.endpoint == body.endpoint.strip()
        )
    )
    if row is not None:
        row.active = False
        db.commit()
    return {"ok": True}


@router.get("/unread-count")
async def bell_unread_count(
    db: GetDB,
    session_id: str = Query(...),
) -> dict[str, Any]:
    """Bell badge count — works even when web-push permission was denied."""
    ensure_push_tables(db)
    return {"session_id": session_id, "unread": unread_count(db, session_id)}


@router.get("/log")
async def bell_log(
    db: GetDB,
    session_id: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    ensure_push_tables(db)
    rows = (
        db.execute(
            select(NotificationLogDB)
            .where(NotificationLogDB.session_id == str(session_id))
            .order_by(NotificationLogDB.id.desc())
            .limit(int(limit))
        ).scalars().all()
    )
    return {
        "session_id": session_id,
        "unread": unread_count(db, session_id),
        "items": [
            {
                "id": r.id,
                "kind": r.kind,
                "title": r.title,
                "body": r.body,
                "url": r.url,
                "pushed": r.pushed,
                "read_at": r.read_at.isoformat() if r.read_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/mark-all-read")
async def mark_all_read(
    db: GetDB,
    session_id: str = Query(...),
) -> dict[str, Any]:
    ensure_push_tables(db)
    rows = (
        db.execute(
            select(NotificationLogDB).where(
                NotificationLogDB.session_id == str(session_id),
                NotificationLogDB.read_at.is_(None),
            )
        ).scalars().all()
    )
    now = datetime.now(UTC)
    for r in rows:
        r.read_at = now
    db.commit()
    return {"ok": True, "marked": len(rows)}


@router.post("/test")
async def test_push(
    db: GetDB,
    session_id: str = Query(...),
) -> dict[str, Any]:
    """One-shot proof ping through :func:`dispatch` (bell + web push)."""
    ensure_push_tables(db)
    result = dispatch(
        db,
        session_id=str(session_id),
        kind="test",
        title="FPL Intelligence test push",
        body="If you can read this, self-hosted web push is alive.",
        url="/dashboard",
    )
    return {"ok": True, **result}
