"""Phase 10.2 — Production Telegram bot worker.

Long-running process invoked by the PaaS ``bot`` process type (see the root
``Procfile``). It reads the bot token and allowed user IDs from the environment
(never hardcoded) and starts polling.

If the required configuration is missing the process exits non-zero so the PaaS
health checks / restart policy can surface the misconfiguration clearly.
"""
from __future__ import annotations

import logging
import os
import sys

from fpl_intelligence.config import get_settings
from fpl_intelligence.db.session import SessionLocal
from fpl_intelligence.notifications.telegram_bot import (
    TelegramBot,
    TelegramBotError,
    get_allowed_user_ids,
)

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed = get_allowed_user_ids()
    if not token or not allowed:
        logger.error(
            "TELEGRAM_BOT_TOKEN and at least one TELEGRAM_ALLOWED_USER_IDS must "
            "be set to run the bot worker."
        )
        sys.exit(1)

    try:
        bot = TelegramBot(
            token,
            allowed,
            db_session_factory=SessionLocal,
        )
    except TelegramBotError as exc:
        logger.error("Failed to start Telegram bot: %s", exc)
        sys.exit(1)

    logger.info("Starting Telegram bot for %d allowed user(s).", len(allowed))
    bot.run()


if __name__ == "__main__":
    main()
