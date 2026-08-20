"""Phase 10.2 — Telegram webhook HTTP endpoint.

Exposes ``POST /api/v1/telegram/webhook`` for FaaS deployments (Vercel), where
the long-running polling worker cannot run. The webhook secret is supplied via
the ``secret`` query parameter (embedded in the Telegram ``setWebhook`` URL).
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request

from fpl_intelligence.notifications.telegram_webhook import handle_webhook

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    secret: Annotated[str | None, Query()] = None,
) -> dict[str, bool]:
    """Receive a Telegram ``Update`` and dispatch it to the bot."""
    try:
        update_json = await request.json()
    except Exception as exc:  # noqa: BLE001 - report bad payloads cleanly
        logger.warning("Invalid Telegram webhook payload: %s", exc)
        return {"ok": False}
    result = await handle_webhook(update_json, secret)
    return result
