"""Phase 10.2 — Telegram Bot tests.

All Telegram API calls are mocked; no live network traffic ever leaves this
process. Heavy DB/LLM operations are mocked or run through ``asyncio.to_thread``
boundaries so the event-loop safety is verified without real providers.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.constants import ParseMode

from fpl_intelligence.live_intelligence.report import (
    IntelligenceReport,
    ReportQualitativeAdjustment,
    ReportQuantitativeBaseline,
)
from fpl_intelligence.live_intelligence.scheduling.alerts import (
    Alert,
    AlertGenerator,
    AlertSeverity,
    AlertType,
)
from fpl_intelligence.notifications.telegram_bot import (
    TelegramBot,
    TelegramBotError,
    _escape_html,
    _escape_markdown_v2,
    get_allowed_user_ids,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(player_id: int = 1, subject_ref: str = "Erling Haaland") -> IntelligenceReport:
    pc = ReportQuantitativeBaseline(
        subject_ref=str(player_id),
        player_id=player_id,
        gameweek=1,
        expected_points=6.0,
        expected_minutes=75.0,
        start_probability=0.85,
        floor=2.0,
        ceiling=14.0,
        fixture_count=1,
        display_name=subject_ref,
    )
    return IntelligenceReport(
        task="transfer_recommendation",
        headline="Haaland remains a must-have for GW1",
        prediction_context=pc,
        qualitative_adjustment=ReportQualitativeAdjustment(
            direction="UP",
            magnitude="SIGNIFICANT",
            cited_evidence_refs=[],
            rationale="Strong fixture run-in.",
        ),
        net_assessment="BUY",
        recommendation="Strong buy.",
        confidence=0.88,
        confidence_band="high",
        provider_name="mock",
        model_name="mock-v1",
        is_mock=True,
        generated_at=datetime.now(UTC),
        citations=[],
        unresolved_warnings=[],
        caveats=[],
    )


def _make_update(user_id: int = 1, chat_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    context = MagicMock()
    context.args = args or []
    return context


def _reply_text(update: MagicMock) -> str:
    call = update.effective_message.reply_text.call_args
    if call is None:
        return ""
    text = call.kwargs.get("text", "")
    if not text and call.args:
        text = call.args[0]
    return text


# ---------------------------------------------------------------------------
# get_allowed_user_ids
# ---------------------------------------------------------------------------


class TestGetAllowedUserIds:
    def test_empty_env_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
        assert get_allowed_user_ids() == []

    def test_comma_separated_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1, 2, 3 ")
        assert get_allowed_user_ids() == [1, 2, 3]

    def test_ignores_non_numeric(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,abc, 2")
        assert get_allowed_user_ids() == [1, 2]


# ---------------------------------------------------------------------------
# TelegramBot construction
# ---------------------------------------------------------------------------


class TestTelegramBotConstruction:
    def test_missing_token_raises(self) -> None:
        with pytest.raises(TelegramBotError, match="TELEGRAM_BOT_TOKEN is required"):
            TelegramBot("", [1])

    def test_empty_allowed_ids_raises(self) -> None:
        with pytest.raises(TelegramBotError, match="TELEGRAM_ALLOWED_USER_IDS must contain"):
            TelegramBot("token", [])

    def test_valid_construction(self) -> None:
        bot = TelegramBot("token", [1, 2])
        assert bot._allowed_user_ids == {1, 2}
        assert bot._dry_run is False

    def test_dry_run_flag(self) -> None:
        bot = TelegramBot("token", [1], dry_run=True)
        assert bot._dry_run is True

    def test_handlers_registered(self) -> None:
        bot = TelegramBot("token", [1])
        assert "start" in bot._handlers
        assert "report" in bot._handlers
        assert "alerts" in bot._handlers
        assert "status" in bot._handlers

    def test_default_alert_generator(self) -> None:
        bot = TelegramBot("token", [1])
        assert bot._alert_generator is not None

    def test_custom_alert_generator(self) -> None:
        generator = AlertGenerator()
        bot = TelegramBot("token", [1], alert_generator=generator)
        assert bot._alert_generator is generator


# ---------------------------------------------------------------------------
# _send
# ---------------------------------------------------------------------------


class TestSend:
    async def test_dry_run_prints_and_does_not_call_reply(self) -> None:
        bot = TelegramBot("token", [1], dry_run=True)
        update = _make_update()
        await bot._send(update, "hello")
        update.effective_message.reply_text.assert_not_called()

    async def test_live_mode_calls_reply_text(self) -> None:
        bot = TelegramBot("token", [1], dry_run=False)
        update = _make_update()
        await bot._send(update, "hello", parse_mode=ParseMode.HTML)
        update.effective_message.reply_text.assert_called_once_with(
            "hello", parse_mode=ParseMode.HTML
        )


# ---------------------------------------------------------------------------
# _start
# ---------------------------------------------------------------------------


class TestStart:
    async def test_unauthorized_user_receives_error(self) -> None:
        bot = TelegramBot("token", [1])
        update = _make_update(user_id=99)
        await bot._start(update, _make_context())
        text = _reply_text(update)
        assert "Unauthorized" in text

    async def test_authorized_user_receives_welcome(self) -> None:
        bot = TelegramBot("token", [1])
        update = _make_update(user_id=1)
        await bot._start(update, _make_context())
        text = _reply_text(update)
        assert "FPL Intelligence Bot" in text
        assert "/report" in text


# ---------------------------------------------------------------------------
# _help
# ---------------------------------------------------------------------------


class TestHelp:
    async def test_unauthorized(self) -> None:
        bot = TelegramBot("token", [1])
        update = _make_update(user_id=99)
        await bot._help(update, _make_context())
        text = _reply_text(update)
        assert "Unauthorized" in text

    async def test_authorized_sees_commands(self) -> None:
        bot = TelegramBot("token", [1])
        update = _make_update(user_id=1)
        await bot._help(update, _make_context())
        text = _reply_text(update)
        assert "/report" in text
        assert "/alerts" in text
        assert "/status" in text


# ---------------------------------------------------------------------------
# _report
# ---------------------------------------------------------------------------


class TestReport:
    async def test_unauthorized(self) -> None:
        bot = TelegramBot("token", [1])
        update = _make_update(user_id=99)
        await bot._report(update, _make_context(args=["Haaland"]))
        text = _reply_text(update)
        assert "Unauthorized" in text

    async def test_missing_args(self) -> None:
        bot = TelegramBot("token", [1])
        update = _make_update(user_id=1)
        await bot._report(update, _make_context(args=[]))
        text = _reply_text(update)
        assert "Usage: /report" in text

    async def test_numeric_player_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report_mock = MagicMock()
        report_mock.generate = MagicMock(return_value=_make_report(1, "Haaland"))
        bot = TelegramBot("token", [1], report_generator=report_mock)
        update = _make_update(user_id=1)
        await bot._report(update, _make_context(args=["4"]))
        assert update.effective_message.reply_text.call_count >= 2

    async def test_name_resolution_falls_back_to_generator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report_mock = MagicMock()
        report_mock.generate = MagicMock(return_value=_make_report(1, "Haaland"))
        bot = TelegramBot(
            "token",
            [1],
            db_session_factory=lambda: _FakeDbSession(no_match=True),
            report_generator=report_mock,
        )
        update = _make_update(user_id=1)
        await bot._report(update, _make_context(args=["Haaland"]))
        text = _reply_text(update)
        assert "Player not found" in text

    async def test_report_generation_error(self) -> None:
        report_mock = MagicMock()
        report_mock.generate = MagicMock(side_effect=RuntimeError("boom"))
        bot = TelegramBot("token", [1], report_generator=report_mock)
        update = _make_update(user_id=1)
        await bot._report(update, _make_context(args=["4"]))
        text = _reply_text(update)
        assert "Error generating report" in text


# ---------------------------------------------------------------------------
# _alerts
# ---------------------------------------------------------------------------


class TestAlerts:
    async def test_unauthorized(self) -> None:
        bot = TelegramBot("token", [1])
        update = _make_update(user_id=99)
        await bot._alerts(update, _make_context())
        text = _reply_text(update)
        assert "Unauthorized" in text

    async def test_no_alerts_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = TelegramBot("token", [1])
        monkeypatch.setattr(bot, "_fetch_high_severity_alerts", lambda: [])
        update = _make_update(user_id=1)
        await bot._alerts(update, _make_context())
        text = _reply_text(update)
        assert "severity alerts" in text

    async def test_returns_up_to_10_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = TelegramBot("token", [1])
        now = datetime.now(UTC)
        alerts = [
            Alert(
                alert_type=AlertType.INJURY,
                severity=AlertSeverity.HIGH,
                title=f"Alert {i}",
                body="body text",
                source_id="test",
                created_at=now,
            )
            for i in range(15)
        ]
        monkeypatch.setattr(bot, "_fetch_high_severity_alerts", lambda: alerts)
        update = _make_update(user_id=1)
        await bot._alerts(update, _make_context())
        final_text = _reply_text(update)
        assert "Alert 9" in final_text
        assert "Alert 14" not in final_text


# ---------------------------------------------------------------------------
# _status
# ---------------------------------------------------------------------------


class TestStatus:
    async def test_unauthorized(self) -> None:
        bot = TelegramBot("token", [1])
        update = _make_update(user_id=99)
        await bot._status(update, _make_context())
        text = _reply_text(update)
        assert "Unauthorized" in text

    async def test_authorized_receives_status(self) -> None:
        bot = TelegramBot("token", [1])
        update = _make_update(user_id=1)
        await bot._status(update, _make_context())
        text = _reply_text(update)
        assert "System Status" in text


# ---------------------------------------------------------------------------
# _resolve_player_id
# ---------------------------------------------------------------------------


class TestResolvePlayerId:
    def test_no_session_factory_returns_none(self) -> None:
        bot = TelegramBot("token", [1])
        assert bot._resolve_player_id("any") is None

    def test_name_match(self) -> None:
        bot = TelegramBot("token", [1], db_session_factory=lambda: _FakeDbSession())
        result = bot._resolve_player_id("Haaland")
        assert result == 1

    def test_no_match_returns_none(self) -> None:
        bot = TelegramBot("token", [1], db_session_factory=lambda: _FakeDbSession(no_match=True))
        assert bot._resolve_player_id("Unknown") is None


class _FakeDbSession:
    def __init__(self, no_match: bool = False) -> None:
        self._no_match = no_match

    def close(self) -> None:
        pass

    def execute(self, stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None if self._no_match else MagicMock(id=1)
        return result


# ---------------------------------------------------------------------------
# _generate_report / _fetch_high_severity_alerts
# ---------------------------------------------------------------------------


class TestWorkerThreads:
    def test_generate_report_delegates_to_generator(self) -> None:
        generator = MagicMock()
        generator.generate.return_value = _make_report(1, "Haaland")
        bot = TelegramBot("token", [1], report_generator=generator)
        report = bot._generate_report(1, "Haaland")
        assert report.prediction_context.subject_ref == "1"

    def test_fetch_alerts_requires_db(self) -> None:
        bot = TelegramBot("token", [1], db_session_factory=lambda: _FakeEmptyDb())
        assert bot._fetch_high_severity_alerts() == []


class _FakeEmptyDb:
    def close(self) -> None:
        pass

    def execute(self, stmt):
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result


# ---------------------------------------------------------------------------
# _build_status_text
# ---------------------------------------------------------------------------


class TestBuildStatusText:
    def test_empty_registries(self) -> None:
        bot = TelegramBot("token", [1])
        text = bot._build_status_text()
        assert "System Status" in text
        assert "No health checks registered" in text
        assert "No metrics registered" in text

    def test_populated_registries(self) -> None:
        bot = TelegramBot("token", [1])
        bot._health.report("db", ok=True)
        metric = bot._metrics.gauge("queue_depth")
        metric.value = 3
        text = bot._build_status_text()
        assert "db" in text
        assert "3" in text


# ---------------------------------------------------------------------------
# _format_report_html
# ---------------------------------------------------------------------------


class TestFormatReportHtml:
    def test_renders_headline(self) -> None:
        bot = TelegramBot("token", [1])
        report = _make_report(1, "Erling Haaland")
        html = bot._format_report_html(report)
        assert "<b>" in html
        assert "Haaland remains a must-have for GW1" in html

    def test_escapes_html(self) -> None:
        bot = TelegramBot("token", [1])
        report = _make_report()
        escaped_headline = _escape_html(report.headline)
        html = bot._format_report_html(report)
        assert escaped_headline in html

    def test_does_not_escape_numeric_confidence(self) -> None:
        bot = TelegramBot("token", [1])
        report = _make_report()
        html = bot._format_report_html(report)
        assert "0.88" in html


# ---------------------------------------------------------------------------
# simulate_command
# ---------------------------------------------------------------------------


class TestSimulateCommand:
    async def test_unknown_command(self, capsys: pytest.CaptureFixture) -> None:
        bot = TelegramBot("token", [1], dry_run=True)
        await bot.simulate_command("unknown", "")
        captured = capsys.readouterr()
        assert "Unknown command" in captured.out

    async def test_dry_run_start_command(self, capsys: pytest.CaptureFixture) -> None:
        bot = TelegramBot("token", [1], dry_run=True)
        await bot.simulate_command("start", "")
        captured = capsys.readouterr()
        assert "FPL Intelligence Bot" in captured.out

    async def test_live_mode_handler_sends_message(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        bot = TelegramBot("token", [1], dry_run=False)
        await bot.simulate_command("start", "")
        captured = capsys.readouterr()
        assert "FPL Intelligence Bot" in captured.out


# ---------------------------------------------------------------------------
# _escape_html / _escape_markdown_v2
# ---------------------------------------------------------------------------


class TestEscapeHelpers:
    def test_html_escapes_ampersand(self) -> None:
        assert _escape_html("a&b") == "a&amp;b"

    def test_html_escapes_angle_brackets(self) -> None:
        assert _escape_html("a<b>") == "a&lt;b&gt;"

    def test_markdown_v2_escapes_special(self) -> None:
        result = _escape_markdown_v2("bold *text*")
        assert "\\*" in result

    def test_markdown_v2_no_double_escape(self) -> None:
        plain = "hello world"
        assert _escape_markdown_v2(plain) == plain


# ---------------------------------------------------------------------------
# run / run_dry_repl integration
# ---------------------------------------------------------------------------


class TestRunDryRepl:
    async def test_live_run_raises_in_dry_mode(self) -> None:
        bot = TelegramBot("token", [1], dry_run=True)
        with pytest.raises(TelegramBotError, match="Cannot run in dry-run mode"):
            bot.run()

    async def test_dry_repl_exits_on_quit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = TelegramBot("token", [1], dry_run=True)
        inputs = iter(["quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        await bot.run_dry_repl()

    async def test_dry_repl_dispatches_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = TelegramBot("token", [1], dry_run=True)
        inputs = iter(["/report Haaland", "quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        await bot.run_dry_repl()
