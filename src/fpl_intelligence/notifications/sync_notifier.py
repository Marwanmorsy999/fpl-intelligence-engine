"""Phase 13.5 — outbound Telegram notification for squad auto-sync.

A tiny best-effort push used after a successful queued squad sync. It reads the
already-deployed ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_ALLOWED_USER_IDS`` from
the environment (the same values the Phase 10.2 bot uses) — **no new secret or
config surface is introduced**. Missing config or an upstream Telegram error
only logs a warning and returns ``False``; it never fails the sync itself.
"""

from __future__ import annotations

import logging
import os

import httpx

from fpl_intelligence.notifications.telegram_bot import get_allowed_user_ids

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"


async def send_squad_synced_notification(
    entry_name: str | None,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Push ``✅ Your FPL squad (NAME) is synced — open the dashboard!``.

    Args:
        entry_name: The manager's FPL team name (may be ``None`` pre-season).
        client: Optional ``httpx.AsyncClient`` for tests; a short-lived client
            is created when omitted.

    Returns:
        True if the message was accepted for at least one allowed chat.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = get_allowed_user_ids()
    if not token or not chat_ids:
        return False

    team = entry_name or "your team"
    text = f"✅ Your FPL squad ({team}) is synced — open the dashboard!"

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    delivered = 0
    try:
        for chat_id in chat_ids:
            try:
                resp = await client.post(
                    f"{_TELEGRAM_API}/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
                resp.raise_for_status()
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - best-effort notification
                logger.warning(
                    "Telegram sync notification failed for chat %s: %s", chat_id, exc
                )
        return delivered > 0
    finally:
        if own_client:
            await client.aclose()
