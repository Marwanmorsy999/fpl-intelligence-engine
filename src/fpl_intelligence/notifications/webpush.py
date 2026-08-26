"""Phase 23 Gate 1 (L2) — self-hosted Web Push (no external accounts).

* A VAPID keypair is generated ONCE (``scripts/generate_vapid_keys.py``) and
  supplied through env: ``VAPID_PUBLIC_KEY`` / ``VAPID_PRIVATE_KEY`` /
  ``VAPID_SUBJECT``.
* Subscriptions live in the ``push_subscriptions`` table; every notification
  — sent or bell-only — lands in ``notifications_log`` so the in-app bell
  works even when browser permission was denied.
* :func:`dispatch` is the ONE fan-out used by every trigger
  (goals / prices / brief / graded). Telegram stays parallel.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from fpl_intelligence.db.base import Base

logger = logging.getLogger(__name__)

#: The four user-facing triggers (per-subscription toggles on /connect).
TRIGGERS = ("goals", "prices", "brief", "graded")


class PushSubscriptionDB(Base):
    """One browser push subscription for one session."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)
    #: {"goals": true, "prices": true, "brief": true, "graded": false}
    triggers: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_endpoint"),
    )


class NotificationLogDB(Base):
    """Every notification (bell history) — powers the unread badge."""

    __tablename__ = "notifications_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # goals|prices|brief|graded|test
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    body: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    url: Mapped[str | None] = mapped_column(String(300))
    pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def vapid_configured() -> bool:
    import os

    return bool(
        os.environ.get("VAPID_PUBLIC_KEY", "").strip()
        and os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    )


def vapid_public_key() -> str:
    import os

    return os.environ.get("VAPID_PUBLIC_KEY", "").strip()


def _wants(triggers: dict[str, Any], kind: str) -> bool:
    if kind not in TRIGGERS:
        return True
    return bool((triggers or {}).get(kind, False))


def send_webpush(subscription: dict[str, Any], payload: dict[str, Any]) -> None:
    """One raw webpush send. Raises on failure; caller decides cleanup.

    Uses ``pywebpush`` when importable. Kept behind a function so tests can
    monkeypatch it without network.
    """
    import os

    from pywebpush import WebPushException, webpush  # noqa: PLC0415

    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=os.environ.get("VAPID_PRIVATE_KEY", "").strip(),
            vapid_claims={
                "sub": os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")
            },
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            raise GoneSubscriptionError(str(exc)) from exc
        raise


class GoneSubscriptionError(RuntimeError):
    """The endpoint is gone (404/410) — deactivate the subscription."""


def dispatch(
    db: Any,
    session_id: str,
    kind: str,
    title: str,
    body: str,
    url: str | None = None,
) -> dict[str, Any]:
    """Log the notification ALWAYS (bell), then web-push per subscription.

    The bell never depends on browser permission or VAPID config — the log
    row is written first and marked ``pushed`` only when delivery happened.
    """
    log_row = NotificationLogDB(
        session_id=str(session_id),
        kind=kind,
        title=title[:200],
        body=body[:500],
        url=url,
        pushed=False,
        created_at=datetime.now(UTC),
    )
    db.add(log_row)
    db.flush()

    result: dict[str, Any] = {
        "logged": True,
        "sent": 0,
        "failed": 0,
        "deactivated": 0,
        "vapid_configured": vapid_configured(),
    }

    subs = (
        db.execute(
            select(PushSubscriptionDB).where(
                PushSubscriptionDB.session_id == str(session_id),
                PushSubscriptionDB.active.is_(True),
            )
        ).scalars().all()
    )
    for sub in subs:
        if not _wants(sub.triggers, kind):
            continue
        if not result["vapid_configured"]:
            continue
        try:
            send_webpush(
                {
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                {
                    "title": title[:120],
                    "body": body[:300],
                    "url": url or "/",
                    "kind": kind,
                },
            )
            result["sent"] += 1
            log_row.pushed = True
        except GoneSubscriptionError:
            sub.active = False
            result["deactivated"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad endpoint never stops the rest
            logger.debug("webpush send failed: %s", exc)
            result["failed"] += 1
    db.commit()
    return result


def unread_count(db: Any, session_id: str) -> int:
    rows = db.execute(
        select(NotificationLogDB.id).where(
            NotificationLogDB.session_id == str(session_id),
            NotificationLogDB.read_at.is_(None),
        )
    ).all()
    return len(rows)


def ensure_push_tables(db: Any) -> None:
    """Self-sealing DDL for prod DBs predating Phase 23."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    ddl = (
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id SERIAL PRIMARY KEY,
            session_id VARCHAR(255) NOT NULL,
            endpoint VARCHAR(500) NOT NULL,
            p256dh VARCHAR(255) NOT NULL,
            auth VARCHAR(255) NOT NULL,
            triggers JSONB NOT NULL DEFAULT '{}'::jsonb,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT uq_push_endpoint UNIQUE (endpoint)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notifications_log (
            id SERIAL PRIMARY KEY,
            session_id VARCHAR(255) NOT NULL,
            kind VARCHAR(30) NOT NULL,
            title VARCHAR(200) NOT NULL DEFAULT '',
            body VARCHAR(500) NOT NULL DEFAULT '',
            url VARCHAR(300),
            pushed BOOLEAN NOT NULL DEFAULT FALSE,
            read_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
        """,
    )
    insp = sa_inspect(db.get_bind())
    try:
        for statement in ddl:
            table = statement.split("EXISTS")[1].split("(")[0].strip()
            if insp.has_table(table):
                continue
            db.execute(text(statement))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — sqlite tests pre-create tables
        db.rollback()
        logger.debug("push DDL skipped: %s", exc)
