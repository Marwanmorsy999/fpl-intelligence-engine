"""Phase 10.2 — Telegram webhook HTTP endpoint.

Exposes ``POST /api/v1/telegram/webhook`` for FaaS deployments (Vercel), where
the long-running polling worker cannot run. The webhook secret is supplied via
the ``secret`` query parameter (embedded in the Telegram ``setWebhook`` URL).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from fpl_intelligence.notifications.telegram_webhook import handle_webhook

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    secret: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Receive a Telegram ``Update`` and dispatch it to the bot.

    The response is intentionally ``dict[str, Any]``: ``handle_webhook`` reports
    rejections as ``{"ok": False, "error": "<reason>"}``. Narrowing this to
    ``dict[str, bool]`` made FastAPI raise ``ResponseValidationError`` on the
    string detail, so every wrong-secret probe answered 500 instead of a
    controlled rejection — and Telegram would retry against a 5xx.
    """
    try:
        update_json = await request.json()
    except Exception as exc:  # noqa: BLE001 - report bad payloads cleanly
        logger.warning("Invalid Telegram webhook payload: %s", exc)
        return {"ok": False, "error": "invalid payload"}
    result = await handle_webhook(update_json, secret)
    return result
