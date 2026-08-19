# Phase 10.2 — Telegram Bot Notifications

Delivers FPL intelligence reports and alerts directly to the user via Telegram.
The bot is an additive layer on top of Phase 9 — it does not modify any
quantitative Phases 1–8 code, nor does it change Phase 9 core extraction or
analysis logic.

---

## Setup

1. Create a Telegram Bot via [@BotFather](https://t.me/botfather) and note the
   token.
2. Determine your Telegram user ID (e.g. via [@userinfobot](https://t.me/userinfobot)).
3. Set the required environment variables:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
export TELEGRAM_ALLOWED_USER_IDS="123456789"
```

Both variables are **required**. `TELEGRAM_ALLOWED_USER_IDS` accepts a
comma-separated list of numeric Telegram user IDs. Any user whose ID is not in
this list receives an `Unauthorized.` response and cannot access bot commands.

> **Security note:** Never commit these values to version control. Store them in
> a git-ignored `.env` file or your deployment secrets manager.

---

## CLI

```bash
# Dry-run interactive REPL (no network calls)
python scripts/run_telegram_bot.py --dry-run

# Live polling mode
python scripts/run_telegram_bot.py
```

### Arguments

| flag | env var | default | description |
|------|---------|---------|-------------|
| `--token` | `TELEGRAM_BOT_TOKEN` | — | Telegram Bot token (required). |
| `--allowed-user-ids` | `TELEGRAM_ALLOWED_USER_IDS` | — | Comma-separated allowed user IDs (required). |
| `--dry-run` | — | `False` | Start an interactive REPL that prints responses to stdout instead of calling the Telegram API. |

Exit codes: `0` success, `1` configuration error.

---

## Commands

| command | description |
|---------|-------------|
| `/start` | Welcome message with available commands. |
| `/help` | Command reference. |
| `/report <player_name_or_id>` | Generate an `IntelligenceReport` for the specified player. Accepts a numeric player ID or a fuzzy name match against `web_name` / full name. |
| `/alerts` | Return up to 10 recent high-severity alerts from the last 24 hours. |
| `/status` | System health and monitoring snapshot (Phase 9.8 `HealthRegistry` + `MetricRegistry`). |

---

## Architecture

```
scripts/run_telegram_bot.py
    └── TelegramBot
            ├── _start / _help        (MarkdownV2)
            ├── _report              (HTML for report body; MarkdownV2 for status)
            ├── _alerts              (MarkdownV2)
            ├── _status              (MarkdownV2)
            ├── _resolve_player_id   (SQLAlchemy LIKE query)
            ├── _generate_report     (AnalystReportGenerator → to_thread)
            ├── _fetch_high_severity_alerts (AlertGenerator → to_thread)
            ├── _format_report_html  (IntelligenceReport → Telegram HTML)
            └── simulate_command     (mock Update/Context for dry-run)
```

### Thread safety

All heavy operations (`_resolve_player_id`, `_generate_report`,
`_fetch_high_severity_alerts`) run inside `asyncio.to_thread` so the event loop
is never blocked. Bot handlers remain async-native.

### Mocking in tests

All `Update`, `Context`, and Telegram API calls are mocked in
`tests/unit/test_phase10_2_telegram.py`. No live Telegram API call is ever
made inside `pytest`. The `simulate_command` helper creates mock `Update`/`Context`
objects and dispatches to the registered handler, printing the response text.

---

## Dependencies

- `python-telegram-bot>=21.0,<22` — async-native `ApplicationBuilder` /
  `CommandHandler` pattern (v20+ API).
- No additional runtime dependencies beyond the existing Phase 9 stack.

## Quality gates

- `pytest` — **752 passed, 0 failed, 0 skipped** (full suite).
- `ruff` clean on `src/fpl_intelligence/notifications`,
  `tests/unit/test_phase10_2_telegram.py`, and `scripts/run_telegram_bot.py`.
- `mypy` clean on all new Phase 10.2 modules.
- No new database migrations required.
