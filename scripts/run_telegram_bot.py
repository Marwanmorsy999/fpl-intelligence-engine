#!/usr/bin/env python
"""scripts/run_telegram_bot.py — Phase 10.2 Telegram Bot CLI.

Starts the FPL Intelligence Telegram bot in either live polling mode or
interactive dry-run REPL mode.

Usage
-----
    python scripts/run_telegram_bot.py
    python scripts/run_telegram_bot.py --dry-run
    TELEGRAM_BOT_TOKEN=123:ABC TELEGRAM_ALLOWED_USER_IDS=1 \\
        python scripts/run_telegram_bot.py --dry-run

``--dry-run`` starts an interactive REPL that dispatches bot commands and
prints their responses to the console without ever touching the Telegram API.
All heavy DB/LLM operations still run through ``asyncio.to_thread`` so the
event-loop behaviour is preserved.

Exit codes: ``0`` success, ``1`` configuration error.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(_SRC))

from fpl_intelligence.notifications.telegram_bot import (  # noqa: E402
    TelegramBot,
    TelegramBotError,
)

EXIT_OK = 0
EXIT_USAGE = 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 10.2 — run the FPL Intelligence Telegram bot. "
            "Use --dry-run for an interactive console REPL that never calls the Telegram API."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
    "--dry-run",
    action="store_true",
    help=(
        "Start an interactive REPL that prints responses to stdout "
        "instead of calling the Telegram API."
    ),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram Bot token (or TELEGRAM_BOT_TOKEN env).",
    )
    parser.add_argument(
        "--allowed-user-ids",
        default=os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""),
        help=(
            "Comma-separated Telegram user IDs allowed to use the bot "
            "(or TELEGRAM_ALLOWED_USER_IDS env)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    token = args.token.strip()
    if not token:
        print("USAGE ERROR: TELEGRAM_BOT_TOKEN is required (--token or TELEGRAM_BOT_TOKEN env).")
        return EXIT_USAGE

    raw_ids = args.allowed_user_ids.strip()
    allowed_ids: list[int] = []
    for part in raw_ids.split(","):
        part = part.strip()
        if part.isdigit():
            allowed_ids.append(int(part))

    if not allowed_ids:
        print(
            "USAGE ERROR: TELEGRAM_ALLOWED_USER_IDS must contain at least one numeric user ID "
            "(--allowed-user-ids or TELEGRAM_ALLOWED_USER_IDS env)."
        )
        return EXIT_USAGE

    try:
        bot = TelegramBot(
            token=token,
            allowed_user_ids=allowed_ids,
            dry_run=args.dry_run,
        )
    except TelegramBotError as exc:
        print(f"USAGE ERROR: {exc}")
        return EXIT_USAGE

    if args.dry_run:
        import asyncio

        with contextlib.suppress(KeyboardInterrupt, EOFError):
            asyncio.run(bot.run_dry_repl())
        return EXIT_OK

    try:
        bot.run()
    except TelegramBotError as exc:
        print(f"RUNTIME ERROR: {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        pass
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
