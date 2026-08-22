"""Serverless Telegram webhook adapter.

Vercel (and other FaaS platforms) cannot run the long-lived polling worker from
``run_telegram_bot.py``. Instead Telegram POSTs each ``Update`` as JSON to this
endpoint. We validate a shared secret, build the :class:`TelegramBot` once per
cold container, and forward the update through its existing command handlers —
no handler logic is duplicated.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fpl_intelligence.db.session import SessionLocal
from fpl_intelligence.notifications.telegram_bot import (
    TelegramBot,
    TelegramBotError,
    get_allowed_user_ids,
)

logger = logging.getLogger(__name__)

_bot: TelegramBot | None = None


def _build_bot() -> TelegramBot | None:
    """Construct the bot from the environment, or ``None`` when unconfigured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed = get_allowed_user_ids()
    if not token or not allowed:
        logger.warning("Telegram webhook disabled: token or allowed users unset.")
        return None
    try:
        return TelegramBot(token, allowed, db_session_factory=SessionLocal)
    except TelegramBotError as exc:
        logger.error("Failed to build Telegram bot: %s", exc)
        return None


def get_bot() -> TelegramBot | None:
    """Return a cached bot instance (one per cold container)."""
    global _bot
    if _bot is None:
        _bot = _build_bot()
    return _bot


async def handle_webhook(update_json: dict[str, Any], secret: str | None) -> dict[str, Any]:
    """Validate ``secret`` and dispatch ``update_json`` to the bot.

    Returns a result dict with ``ok`` and, on failure, an ``error`` detail so
    callers (and cron/manual testers) can see what went wrong.
    """
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if expected and secret != expected:
        logger.warning("Telegram webhook secret mismatch; rejecting update.")
        return {"ok": False, "error": "secret mismatch"}

    bot = get_bot()
    if bot is None:
        return {"ok": False, "error": "bot not configured"}

    try:
        await bot.process_webhook_update(update_json)
    except Exception as exc:  # noqa: BLE001 - never crash the webhook handler
        logger.exception("Telegram webhook processing failed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True}
