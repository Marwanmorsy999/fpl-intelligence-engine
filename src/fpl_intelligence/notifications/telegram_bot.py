"""Phase 10.2 — Telegram Bot Integration for Alerts and Reports.

Delivers FPL intelligence directly to the user via Telegram. The bot exposes
commands for on-demand intelligence reports and automated alerts, using the
existing Phase 9.4 ``AnalystReportGenerator``, Phase 9.6 ``AlertGenerator``,
and Phase 9.8 ``HealthRegistry``/``MetricRegistry``.

Design rules
------------

* No quantitative Phases 1–8 code is modified.
* No Phase 9 core extraction/analysis logic is modified.
* Heavy DB/LLM work is offloaded to thread pools so bot handlers never block
  the event loop.
* The Telegram Bot Token and allowed user IDs are read from environment
  variables — never hardcoded.
* All Telegram API calls are mocked in unit tests; no live network call is
  ever made inside ``pytest``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from fpl_intelligence.deployment.monitoring import HealthRegistry, MetricRegistry
from fpl_intelligence.live_intelligence.report import IntelligenceReport
from fpl_intelligence.live_intelligence.scheduling.alerts import (
    Alert,
    AlertGenerator,
    AlertSeverity,
)

logger = logging.getLogger(__name__)


class TelegramBotError(RuntimeError):
    """Raised when the Telegram bot encounters a fatal configuration or runtime error."""


def get_allowed_user_ids() -> list[int]:
    """Read comma-separated allowed user IDs from the environment.

    Returns an empty list when the variable is unset or contains no valid IDs.
    """
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def _escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    result = ""
    for char in text:
        if char in special:
            result += "\\" + char
        else:
            result += char
    return result


def _escape_html(text: str) -> str:
    """Escape special characters for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramBot:
    """Async Telegram bot that delivers FPL intelligence reports and alerts.

    Args:
        token: Telegram Bot token (from ``TELEGRAM_BOT_TOKEN``).
        allowed_user_ids: List of Telegram user IDs allowed to use the bot.
        report_generator: Optional ``AnalystReportGenerator`` for ``/report``.
        alert_generator: Optional ``AlertGenerator`` for ``/alerts``.
        db_session_factory: Callable that returns a SQLAlchemy ``Session``.
            Used for player name resolution and fetching recent raw items.
        dry_run: When ``True``, outgoing messages are printed to the console
            instead of being sent via the Telegram API.
    """

    def __init__(
        self,
        token: str,
        allowed_user_ids: list[int],
        *,
        report_generator: Any | None = None,
        alert_generator: AlertGenerator | None = None,
        db_session_factory: Callable[[], Any] | None = None,
        dry_run: bool = False,
    ) -> None:
        if not token:
            raise TelegramBotError("TELEGRAM_BOT_TOKEN is required")
        if not allowed_user_ids:
            raise TelegramBotError("TELEGRAM_ALLOWED_USER_IDS must contain at least one user ID")

        self._token = token
        self._allowed_user_ids = set(allowed_user_ids)
        self._report_generator = report_generator
        self._alert_generator = alert_generator or AlertGenerator()
        self._db_session_factory = db_session_factory
        self._dry_run = dry_run

        self._health = HealthRegistry()
        self._metrics = MetricRegistry()
        self._application = ApplicationBuilder().token(token).build()
        self._handlers: dict[str, Any] = {}
        self._webhook_ready = False
        self._register_handlers()

    def _register_handlers(self) -> None:
        self._handlers = {
            "start": self._start,
            "help": self._help,
            "report": self._report,
            "alerts": self._alerts,
            "status": self._status,
        }
        self._application.add_handler(CommandHandler("start", self._start))
        self._application.add_handler(CommandHandler("help", self._help))
        self._application.add_handler(CommandHandler("report", self._report))
        self._application.add_handler(CommandHandler("alerts", self._alerts))
        self._application.add_handler(CommandHandler("status", self._status))

    async def _send(self, update: Update, text: str, **kwargs: Any) -> None:
        """Send a message or print it to console in dry-run mode."""
        if self._dry_run:
            chat_id = (
                update.effective_chat.id if update.effective_chat else "unknown"
            )
            print(f"[DRY RUN] chat={chat_id}: {text}")
            return
        effective_message = update.effective_message
        if effective_message is not None:
            await effective_message.reply_text(text, **kwargs)

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in self._allowed_user_ids:
            await self._send(
                update,
                "Unauthorized\\. Your user ID is not in the allowed list\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        welcome = (
            "*FPL Intelligence Bot*\n\n"
            "Available commands:\n"
            "/report \\<player\\> \\- Generate intelligence report for a player\n"
            "/alerts \\- Recent high\\-severity alerts\n"
            "/status \\- System health status\n"
            "/help \\- Show this message"
        )
        await self._send(update, welcome, parse_mode=ParseMode.MARKDOWN_V2)

    async def _help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in self._allowed_user_ids:
            await self._send(update, "Unauthorized\\.")
            return
        help_text = (
            "*Commands*\n\n"
            "/report \\<player\\_name\\_or\\_id\\> \\- Player name or ID\\.\n"
            "  Generates an intelligence report\\.\n"
            "/alerts \\- List recent high\\-severity alerts\\.\n"
            "/status \\- System health and monitoring status\\.\n"
            "/help \\- This help message\\."
        )
        await self._send(update, help_text, parse_mode=ParseMode.MARKDOWN_V2)

    async def _report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in self._allowed_user_ids:
            await self._send(update, "Unauthorized\\.")
            return
        if not context.args or not context.args[0]:
            await self._send(
                update,
                "Usage: /report \\<player\\_name\\_or\\_id\\>",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        query = context.args[0]
        try:
            player_id = int(query)
        except ValueError:
            player_id = await asyncio.to_thread(
                self._resolve_player_id, query  # type: ignore[arg-type]
            )

        if player_id is None:
            await self._send(
                update,
                f"Player not found: {_escape_markdown_v2(query)}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        await self._send(
            update,
            f"Generating report for player {_escape_markdown_v2(str(player_id))}\\.\\.\\.",
        )

        try:
            report = await asyncio.to_thread(
                self._generate_report, player_id, query
            )
        except Exception as exc:
            logger.exception("report generation failed for player %s", player_id)
            await self._send(
                update,
                f"Error generating report: {_escape_markdown_v2(str(exc))}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        md = self._format_report_html(report)
        await self._send(update, md, parse_mode=ParseMode.HTML)

    async def _alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in self._allowed_user_ids:
            await self._send(update, "Unauthorized\\.")
            return

        await self._send(update, "Fetching recent high\\-severity alerts\\.\\.\\.")

        try:
            alerts = await asyncio.to_thread(self._fetch_high_severity_alerts)
        except Exception as exc:
            logger.exception("alert fetch failed")
            await self._send(
                update,
                f"Error fetching alerts: {_escape_markdown_v2(str(exc))}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        if not alerts:
            await self._send(
                update,
                "No high\\-severity alerts found\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        lines = ["*Recent High\\-Severity Alerts*", ""]
        for alert in alerts[:10]:
            title = _escape_markdown_v2(alert.title)
            lines.append(f"\\- \\[{alert.severity.value.upper()}\\] {title}")
            if alert.body:
                body = _escape_markdown_v2(alert.body[:200])
                lines.append(f"  {body}")
            lines.append("")

        await self._send(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

    async def _status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in self._allowed_user_ids:
            await self._send(update, "Unauthorized\\.")
            return

        status_text = self._build_status_text()
        await self._send(update, status_text, parse_mode=ParseMode.MARKDOWN_V2)

    def _resolve_player_id(self, name: str) -> int | None:
        """Resolve a player name or web name to a canonical player ID."""
        if self._db_session_factory is None:
            return None
        db = self._db_session_factory()
        try:
            from sqlalchemy import func, select

            from fpl_intelligence.db.models import Player

            pattern = f"%{name}%"
            stmt = (
                select(Player)
                .where(
                    (func.lower(Player.web_name).like(func.lower(pattern)))
                    | (
                        func.lower(Player.first_name + " " + Player.second_name).like(
                            func.lower(pattern)
                        )
                    )
                )
                .limit(1)
            )
            result = db.execute(stmt).scalar_one_or_none()
            return result.id if result else None
        finally:
            db.close()

    def _generate_report(self, player_id: int, subject_label: str) -> IntelligenceReport:
        """Generate an intelligence report (runs in a worker thread)."""
        if self._report_generator is None:
            raise TelegramBotError("report_generator is not configured")
        return self._report_generator.generate(
            player_id=player_id,
            gameweek=1,
            subject_label=subject_label,
        )

    def _fetch_high_severity_alerts(self) -> list[Alert]:
        """Query recent raw items and return high-severity alerts (runs in a worker thread)."""
        if self._db_session_factory is None:
            return []
        db = self._db_session_factory()
        try:
            from datetime import timedelta

            from sqlalchemy import select

            from fpl_intelligence.live_intelligence.models import LiveIntelligenceRawItem

            cutoff = datetime.now(UTC) - timedelta(hours=24)
            stmt = (
                select(LiveIntelligenceRawItem)
                .where(LiveIntelligenceRawItem.ingested_at >= cutoff)
                .order_by(LiveIntelligenceRawItem.ingested_at.desc())
                .limit(50)
            )
            rows = db.execute(stmt).scalars().all()
            report = self._alert_generator.generate(rows)
            return [a for a in report.alerts if a.severity == AlertSeverity.HIGH]
        finally:
            db.close()

    def _build_status_text(self) -> str:
        health = self._health.snapshot()
        metrics = self._metrics.snapshot()
        lines = [
            "*System Status*",
            "",
            f"Health: {len(health)} component\\(s\\) registered",
            f"Metrics: {len(metrics)} metric\\(s\\) registered",
            "",
            "*Health Checks*",
        ]
        if health:
            for name, check in health.items():
                status = "OK" if check.ok else "DOWN"
                lines.append(f"\\- {_escape_markdown_v2(name)}: {status}")
        else:
            lines.append("No health checks registered\\.")
        lines.append("")
        lines.append("*Metrics*")
        if metrics:
            for name, metric in metrics.items():
                lines.append(f"\\- {_escape_markdown_v2(name)}: {metric.value}")
        else:
            lines.append("No metrics registered\\.")
        return "\n".join(lines)

    def _format_report_html(self, report: IntelligenceReport) -> str:
        """Render an ``IntelligenceReport`` as Telegram-compatible HTML."""
        lines: list[str] = []
        pc = report.prediction_context
        label = _escape_html(pc.display_name or pc.subject_ref)

        lines.append(f"<b>{_escape_html(report.headline)}</b>")
        lines.append("")
        lines.append(f"<b>Task:</b> {_escape_html(report.task)}")
        lines.append(f"<b>Recommendation:</b> {_escape_html(report.recommendation)}")
        lines.append(
            f"<b>Confidence:</b> {report.confidence:.2f} ({_escape_html(report.confidence_band)})"
        )
        if report.provider_name or report.model_name:
            mock_tag = " (mock)" if report.is_mock else ""
            provider = _escape_html(report.provider_name or "unknown")
            model = _escape_html(report.model_name or "unknown")
            lines.append(f"<b>Provider:</b> {provider} / {model}{mock_tag}")
        lines.append("")
        if report.generated_at:
            lines.append(f"<i>Generated: {report.generated_at.isoformat()}</i>")
            lines.append("")

        lines.append("<b>Quantitative Baseline</b>")
        lines.append("")
        lines.append(f"Player: {label} ({_escape_html(pc.subject_ref)})")
        lines.append(f"Gameweek: {pc.gameweek}")
        lines.append(f"Fixtures: {pc.fixture_count}")
        lines.append(f"Expected points: {pc.expected_points}")
        lines.append(f"Expected minutes: {pc.expected_minutes}")
        lines.append(f"Start probability: {pc.start_probability:.2%}")
        lines.append(f"Floor (P10): {pc.floor}")
        lines.append(f"Ceiling (P90): {pc.ceiling}")
        lines.append("")

        lines.append("<b>Qualitative Assessment</b>")
        lines.append("")
        adj = report.qualitative_adjustment
        lines.append(f"<b>Direction:</b> {_escape_html(adj.direction)}")
        lines.append(f"<b>Magnitude:</b> {_escape_html(adj.magnitude)}")
        if adj.cited_evidence_refs:
            refs = ", ".join(
                f"<code>{_escape_html(r)}</code>" for r in adj.cited_evidence_refs
            )
            lines.append(f"<b>Evidence refs:</b> {refs}")
        lines.append("")
        if adj.rationale:
            lines.append(f"<i>{_escape_html(adj.rationale)}</i>")
            lines.append("")

        if report.citations:
            lines.append("<b>Evidence Cited</b>")
            lines.append("")
            for c in report.citations:
                summary = _escape_html(c.summary.replace("|", "/")[:80])
                lines.append(
                    f"<code>{_escape_html(c.evidence_ref)}</code> | {_escape_html(c.kind)} | "
                    f"{summary} | {_escape_html(c.source_name)} | {c.confidence:.2f}"
                )
            lines.append("")

        if report.unresolved_warnings:
            lines.append("<b>Unresolved Warnings</b>")
            lines.append("")
            for w in report.unresolved_warnings:
                hint = _escape_html(w.subject_hint or "(unnamed)")
                lines.append(
                    f"<code>{_escape_html(w.evidence_ref)}</code> ({_escape_html(w.kind)}): "
                    f"<b>{hint}</b> — {_escape_html(w.resolution_status)}: "
                    f"{_escape_html(w.resolution_reason)}"
                )
            lines.append("")

        if report.net_assessment:
            lines.append("<b>Net Assessment</b>")
            lines.append("")
            lines.append(_escape_html(report.net_assessment))
            lines.append("")

        if report.caveats:
            lines.append("<b>Caveats</b>")
            lines.append("")
            for caveat in report.caveats:
                lines.append(f"- {_escape_html(caveat)}")
            lines.append("")

        return "\n".join(lines)

    async def simulate_command(
        self, command: str, args: str, user_id: int = 1
    ) -> None:
        """Simulate a Telegram command for dry-run testing.

        Creates mock ``Update`` and ``Context`` objects and dispatches to the
        registered handler. Responses are printed to stdout.
        """
        from unittest.mock import AsyncMock, MagicMock

        update = MagicMock()
        update.effective_user.id = user_id
        update.effective_chat.id = user_id
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = [args] if args else []

        handler = self._handlers.get(command)
        if handler is None:
            print(f"[DRY RUN] Unknown command: /{command}")
            return

        await handler(update, context)

        if update.effective_message.reply_text.called:
            call_kwargs = update.effective_message.reply_text.call_args.kwargs
            text = call_kwargs.get("text", "")
            if not text and update.effective_message.reply_text.call_args.args:
                text = update.effective_message.reply_text.call_args.args[0]
            print(f"[DRY RUN] chat={user_id}: {text}")

    async def run_dry_repl(self) -> None:
        """Interactive REPL for dry-run mode.

        Reads commands from stdin, dispatches them through the bot handlers,
        and prints responses to the console.
        """
        print("Starting Telegram bot in DRY-RUN mode...")
        print(f"Allowed user IDs: {sorted(self._allowed_user_ids)}")
        print("Type commands like '/report Haaland' or '/status' (Ctrl+C to stop)")

        loop = asyncio.get_running_loop()
        try:
            while True:
                line = await loop.run_in_executor(None, input, "> ")
                line = line.strip()
                if not line:
                    continue
                if line.lower() in ("exit", "quit"):
                    break
                parts = line.split(maxsplit=1)
                command = parts[0].lstrip("/")
                args = parts[1] if len(parts) > 1 else ""
                await self.simulate_command(command, args, user_id=1)
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            print("\nDry-run stopped.")

    async def process_webhook_update(self, update_json: dict[str, Any]) -> None:
        """Process a single update received via a webhook (serverless / Vercel).

        Initialises the underlying ``python-telegram-bot`` application once, then
        feeds the parsed ``Update`` through the same command handlers used by the
        polling worker. No long-running loop is started.
        """
        application = self._application
        if not self._webhook_ready:
            await application.initialize()
            self._webhook_ready = True
        update = Update.de_json(update_json, application.bot)
        await application.process_update(update)

    def run(self) -> None:
        """Start the bot polling loop (live mode only)."""
        if self._dry_run:
            raise TelegramBotError(
                "Cannot run in dry-run mode. Use run_dry_repl() instead."
            )
        self._application.run_polling()
