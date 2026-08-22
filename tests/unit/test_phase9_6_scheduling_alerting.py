"""Phase 9.6 unit tests — Scheduling and Alerting.

Covers the :class:`Scheduler` (fetch → ingest → alert → notify orchestration
with mocked HTTP responses via ``httpx.MockTransport``), the
:class:`AlertGenerator` (offline heuristic classification, rate limiting and
per-item error isolation), and the :class:`NotificationService` + notifiers
(Slack HTTP mocked, Email SMTP injected, log / recording sinks). **No live
network call is ever made inside ``pytest``.**
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from fpl_intelligence.live_intelligence.connectors import (
    FPLAPIConnector,
    RSSConnector,
    SourceConnectionError,
    SourceConnector,
)
from fpl_intelligence.live_intelligence.raw_item_ledger import RawItem
from fpl_intelligence.live_intelligence.scheduling import (
    Alert,
    AlertGenerationReport,
    AlertGenerator,
    AlertSeverity,
    AlertType,
    EmailNotifier,
    LogNotifier,
    NotificationError,
    NotificationReceipt,
    NotificationService,
    Notifier,
    RecordingNotifier,
    Scheduler,
    SlackNotifier,
    classify_alert_type,
    render_notification,
)

NOW = datetime(2025, 8, 16, 12, 0, 0, tzinfo=UTC)

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Salah injury doubt</title>
      <description>Mohamed Salah missed training with a hamstring injury.</description>
      <link>https://example.com/salah</link>
      <guid>guid-1</guid>
      <pubDate>Thu, 14 Aug 2025 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

FPL_PAYLOAD = {
    "elements": [
        {
            "id": 1,
            "web_name": "Salah",
            "first_name": "Mohamed",
            "second_name": "Salah",
            "news": "Hamstring injury — doubt for the next round",
            "chance_of_playing_next_round": 50,
            "chance_of_playing_this_round": 75,
        },
        {
            "id": 2,
            "web_name": "Haaland",
            "first_name": "Erling",
            "second_name": "Haaland",
            "news": "",
            "chance_of_playing_next_round": 100,
            "chance_of_playing_this_round": 100,
        },
    ]
}


def _noop_sleep(_seconds: float) -> None:
    return None


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Build an httpx.Client whose transport is entirely mocked (no network)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _raw(
    title: str,
    content: str,
    *,
    source_id: str = "rss",
    external_id: str | None = None,
) -> RawItem:
    return RawItem.create(
        source_id=source_id,
        title=title,
        content_text=content,
        published_at=NOW,
        scraped_at=NOW,
        ingested_at=NOW,
        url="https://example.com/item",
        external_id=external_id,
    )


def _rss_connector(
    *,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> RSSConnector:
    h = handler or (lambda _request: httpx.Response(200, text=RSS_XML))
    return RSSConnector(
        "https://example.com/feed.rss",
        http_client=_make_client(h),
        clock=lambda: NOW,
        monotonic_clock=lambda: 0.0,
        sleep=_noop_sleep,
    )


def _fpl_connector(
    *,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> FPLAPIConnector:
    h = handler or (lambda _request: httpx.Response(200, json=FPL_PAYLOAD))
    return FPLAPIConnector(
        api_url="https://example.com/bootstrap-static/",
        http_client=_make_client(h),
        clock=lambda: NOW,
        monotonic_clock=lambda: 0.0,
        sleep=_noop_sleep,
    )


class _MockConnector(SourceConnector):
    name = "mock"
    source_id = "mock"

    def __init__(
        self,
        items: list[RawItem] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        super().__init__(
            clock=lambda: NOW,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        self._items = items or []
        self._error = error
        self.fetch_calls = 0

    def fetch(self, *, limit: int | None = None) -> list[RawItem]:
        self.fetch_calls += 1
        if self._error is not None:
            raise self._error
        return list(self._items)


def _noop_ingest(raw: RawItem, *, connector: SourceConnector, dry_run: bool) -> dict[str, Any]:
    return {"ok": True, "external_id": raw.external_id}


def _ingest(seen: list[tuple[str, str | None, bool]]):
    """Build a recording Phase 9.2 ingestion sink."""

    def ingest(raw: RawItem, *, connector: SourceConnector, dry_run: bool) -> dict[str, Any]:
        seen.append((connector.name, raw.external_id, dry_run))
        return {"ok": True, "external_id": raw.external_id}

    return ingest


class TestScheduler:
    """Scheduler tests — connector HTTP responses are mocked via MockTransport."""

    def _scheduler(
        self,
        connectors: dict[str, SourceConnector],
        *,
        ingest: Callable[..., Any] | None = None,
        alert_generator: AlertGenerator | None = None,
        notification_service: NotificationService | None = None,
        **kwargs,
    ) -> Scheduler:
        return Scheduler(
            connectors,
            ingest=ingest or _noop_ingest,
            alert_generator=alert_generator,
            notification_service=notification_service,
            **kwargs,
        )

    def test_run_fetches_and_ingests_all_connectors(self):
        seen: list[tuple[str, str | None, bool]] = []
        scheduler = self._scheduler(
            {"rss": _rss_connector(), "fpl_api": _fpl_connector()},
            ingest=_ingest(seen),
        )
        report = scheduler.run()
        assert report.connector_report.total_fetched == 2
        assert report.connector_report.total_ingested == 2
        assert report.connector_report.connectors_ran == 2
        assert seen == [("rss", "guid-1", False), ("fpl_api", "1", False)]

    def test_run_selects_single_connector(self):
        a = _MockConnector([_raw("a", "x", external_id="1")])
        b = _MockConnector([_raw("b", "y", external_id="2")])
        a.name, b.name = "a", "b"
        seen: list[tuple[str, str | None, bool]] = []
        scheduler = self._scheduler({"a": a, "b": b}, ingest=_ingest(seen))
        report = scheduler.run(connector="a")
        assert report.connector_report.connectors_ran == 1
        assert seen == [("a", "1", False)]

    def test_run_unknown_connector_raises(self):
        scheduler = self._scheduler({"c": _MockConnector([])})
        with pytest.raises(KeyError):
            scheduler.run(connector="nope")

    def test_fetch_error_is_isolated_per_connector(self):
        ok = _MockConnector([_raw("ok", "x", external_id="1")])
        ok.name = "ok"
        broken = _MockConnector([], error=SourceConnectionError("feed down"))
        scheduler = self._scheduler({"ok": ok, "broken": broken}, ingest=_ingest([]))
        report = scheduler.run()
        assert report.connector_report.total_fetched == 1
        assert report.connector_report.stats_for("ok").fetched == 1
        assert report.connector_report.stats_for("ok").succeeded
        assert not report.connector_report.stats_for("broken").succeeded
        assert "feed down" in report.connector_report.stats_for("broken").errors[0]

    def test_ingest_error_is_isolated_per_item(self):
        items = [
            _raw("c", "good-1", external_id="1"),
            _raw("c", "bad", external_id="bad"),
            _raw("c", "good-2", external_id="2"),
        ]

        def failing_ingest(raw, *, connector, dry_run):
            if raw.external_id == "bad":
                raise RuntimeError("persist failed")
            return {"ok": True, "external_id": raw.external_id}

        scheduler = Scheduler({"c": _MockConnector(items)}, ingest=failing_ingest)
        report = scheduler.run()
        stats = report.connector_report.stats_for("c")
        assert stats.fetched == 3
        assert stats.ingested == 2
        assert len(stats.errors) == 1
        assert "bad" in stats.errors[0]
        assert len(report.ingested_items) == 2

    def test_dry_run_flag_forwarded_to_ingest(self):
        calls: list[bool] = []

        def recorder(raw, *, connector, dry_run):
            calls.append(dry_run)
            return {"ok": True}

        scheduler = self._scheduler({"c": _MockConnector([_raw("x", "1")])}, ingest=recorder)
        scheduler.run(dry_run=True)
        assert calls == [True]

    def test_run_scheduled_runs_passes_and_sleeps(self):
        connector = _MockConnector([_raw("x", "1")])
        sleeps: list[float] = []
        scheduler = self._scheduler(
            {"c": connector},
            sleep=sleeps.append,
        )
        reports = scheduler.run_scheduled(interval_seconds=2.0, iterations=3)
        assert len(reports) == 3
        assert connector.fetch_calls == 3
        assert sleeps == [2.0, 2.0]

    def test_run_scheduled_honours_stop_event(self):
        scheduler = self._scheduler({"c": _MockConnector([])}, ingest=_noop_ingest)
        stop_event = threading.Event()
        stop_event.set()
        reports = scheduler.run_scheduled(interval_seconds=1.0, iterations=5, stop_event=stop_event)
        assert reports == []

    def test_run_paces_passes_with_rate_limiter(self):
        # min_interval_seconds=5 and a monotonic clock frozen at 0 →
        # the second pass sleeps 5s.
        record: list[float] = []
        connectors = {"c": _MockConnector([])}
        scheduler = Scheduler(
            connectors,
            ingest=_noop_ingest,
            min_interval_seconds=5.0,
            monotonic_clock=lambda: 0.0,
            sleep=record.append,
        )
        scheduler.run()
        scheduler.run()
        assert record == [5.0]

    def test_run_report_to_dict(self):
        report = Scheduler({"c": _MockConnector([])}, ingest=_noop_ingest).run()
        d = report.to_dict()
        assert d["connector_report"]["total_fetched"] == 0
        assert d["connector_report"]["connectors_ran"] == 1
        assert d["notifications"] is None
        assert d["alerts"] == []


class TestAlertGenerator:
    """AlertGenerator is offline; no HTTP is involved."""

    def test_classify_injury(self):
        item = _raw("Salah injury doubt", "Salah missed training with a hamstring injury.")
        assert classify_alert_type(item) == AlertType.INJURY

    def test_classify_availability_risk(self):
        item = _raw(
            "Doku fitness doubt",
            "chance_of_playing_next_round: 25 percent",
        )
        assert classify_alert_type(item) == AlertType.AVAILABILITY_RISK

    def test_classify_tactical_change(self):
        item = _raw("Pep tactical switch", "Pep switches to a back three.")
        assert classify_alert_type(item) == AlertType.TACTICAL_CHANGE

    def test_classify_transfer_news(self):
        item = _raw("Haaland transfer news", "City complete the signing of Haaland.")
        assert classify_alert_type(item) == AlertType.TRANSFER_NEWS

    def test_classify_general(self):
        item = _raw("Press conference news", "Pep holds a press conference.")
        assert classify_alert_type(item) == AlertType.GENERAL

    def test_classify_no_match_returns_none(self):
        item = _raw("Quiet day", "Nothing much happened.")
        assert classify_alert_type(item) is None

    def test_generate_builds_alert_with_fields(self):
        generator = AlertGenerator()
        report = generator.generate([_raw("Salah injury", "Salah has a hamstring injury.")])
        assert len(report.alerts) == 1
        alert = report.alerts[0]
        assert alert.alert_type == AlertType.INJURY
        assert alert.severity == AlertSeverity.HIGH
        assert alert.title == "Salah injury"
        assert alert.raw_item_id is None
        assert alert.source_id == "rss"

    def test_generate_respects_limit(self):
        generator = AlertGenerator()
        raw = _raw("a", "injury", external_id="1")
        first = generator.generate([raw, _raw("b", "injury", external_id="2")], limit=1)
        assert len(first.alerts) == 1
        assert first.processed == 1

    def test_generate_isolates_classifier_errors(self):
        def bad_classify(raw):
            if raw.external_id == "boom":
                raise RuntimeError("classifier exploded")
            return AlertType.INJURY

        generator = AlertGenerator(classifier=bad_classify)
        report = generator.generate(
            [
                _raw("ok", "x", external_id="1"),
                _raw("bad", "y", external_id="boom"),
            ]
        )
        assert len(report.errors) == 1

    def test_generate_paces_passes_with_rate_limiter(self):
        record: list[float] = []
        generator = AlertGenerator(
            min_interval_seconds=5.0,
            monotonic_clock=lambda: 0.0,
            sleep=record.append,
        )
        generator.generate([])
        generator.generate([])
        # first call never waits; second call: elapsed 0 < 5 → sleeps 5s
        assert record == [5.0]
        assert generator.rate_limiter.stats.calls == 2

    def test_generate_max_alerts_per_pass_cap(self):
        generator = AlertGenerator(max_alerts_per_pass=2)
        items = [_raw("a", "injury"), _raw("b", "injury"), _raw("c", "injury")]
        report = generator.generate(items)
        assert report.generated == 2
        assert len(report.alerts) == 2
        assert report.processed == 2

    def test_alert_to_dict(self):
        alert = Alert(
            alert_type=AlertType.INJURY,
            severity=AlertSeverity.HIGH,
            title="Salah injury",
            body="Hamstring injury",
            source_id="rss",
            created_at=NOW,
            url="https://example.com/salah",
            external_id="guid-1",
            matched_keywords=("injury", "hamstring"),
        )
        d = alert.to_dict()
        assert d["alert_type"] == "injury"
        assert d["severity"] == "high"
        assert d["title"] == "Salah injury"
        assert d["matched_keywords"] == ["injury", "hamstring"]

    def test_generator_rejects_zero_cap(self):
        with pytest.raises(ValueError):
            AlertGenerator(max_alerts_per_pass=0)


class TestSchedulerAlertAndNotifyStages:
    """Scheduler end-to-end: fetch (mocked HTTP) → ingest → alert → notify."""

    def test_generates_alerts_and_notifies(self):
        recorder = RecordingNotifier()
        service = NotificationService([recorder])
        scheduler = Scheduler(
            {"feed": _rss_connector()},
            ingest=_noop_ingest,
            alert_generator=AlertGenerator(),
            notification_service=service,
        )
        report = scheduler.run()
        assert len(report.alerts) == 1
        assert report.alerts[0].alert_type == AlertType.INJURY
        assert report.alerts[0].severity == AlertSeverity.HIGH
        assert len(recorder.sent) == 1
        assert report.notifications is not None
        assert report.notifications.delivered == 1
        assert report.notifications.failed == 0

    def test_skips_alert_stage_when_disabled(self):
        scheduler = Scheduler(
            {"c": _MockConnector([_raw("Salah injury", "hamstring injury")])},
            ingest=_noop_ingest,
            alert_generator=AlertGenerator(),
            notification_service=NotificationService([RecordingNotifier()]),
        )
        report = scheduler.run(generate_alerts=False)
        assert report.alerts == []
        assert report.notifications is None

    def test_skips_notify_stage_without_alerts(self):
        scheduler = Scheduler(
            {"c": _MockConnector([_raw("Quiet day", "Nothing much.")])},
            ingest=_noop_ingest,
            alert_generator=AlertGenerator(),
            notification_service=NotificationService([RecordingNotifier()]),
        )
        report = scheduler.run()
        assert report.alerts == []
        assert report.notifications is None

    def test_alert_generation_failure_is_captured(self):
        generator = _ExplodingAlertGenerator()
        scheduler = Scheduler(
            {"c": _MockConnector([_raw("Salah injury", "hamstring injury")])},
            ingest=_noop_ingest,
            alert_generator=generator,
        )
        report = scheduler.run()
        assert generator.calls == 1
        assert report.errors == ["alert generation failed: generator exploded"]

    def test_notification_failure_is_captured(self):
        service = _ExplodingNotificationService()
        scheduler = Scheduler(
            {"c": _MockConnector([_raw("Salah injury", "hamstring injury")])},
            ingest=_noop_ingest,
            alert_generator=AlertGenerator(),
            notification_service=service,
        )
        report = scheduler.run()
        assert report.alerts
        assert report.errors == ["notification dispatch failed: notifier exploded"]

    def test_scheduler_run_report_to_dict(self):
        scheduler = Scheduler(
            {"feed": _rss_connector()},
            ingest=_noop_ingest,
            alert_generator=AlertGenerator(),
            notification_service=NotificationService([RecordingNotifier()]),
        )
        report = scheduler.run()
        d = report.to_dict()
        assert d["connector_report"]["total_fetched"] == 1
        assert d["ingested_items"] == 1
        assert d["alerts"][0]["alert_type"] == "injury"
        assert d["notifications"]["delivered"] == 1

    def test_scheduler_supports_single_connector_with_mocked_http(self):
        # Request that only the RSS connector be run, not any additional connectors.
        connectors = {
            "rss": _rss_connector(),
            "fpl_api": _fpl_connector(),
        }
        seen: list[tuple[str, str | None, bool]] = []
        scheduler = Scheduler(connectors, ingest=_ingest(seen))
        report = scheduler.run(connector="rss")
        assert report.connector_report.connectors_ran == 1
        assert seen == [("rss", "guid-1", False)]

        # also run all connectors and verify the API connector's item was fetched too
        report = scheduler.run()
        assert report.connector_report.total_fetched == 2


class _ExplodingAlertGenerator(AlertGenerator):
    """A generator whose ``generate`` always raises (stage-isolation tests)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def generate(self, items: Any, *, limit: int | None = None) -> AlertGenerationReport:
        self.calls += 1
        raise RuntimeError("generator exploded")


class _ExplodingNotificationService(NotificationService):
    """A service whose ``send_alerts`` always raises (stage-isolation tests)."""

    def __init__(self) -> None:
        super().__init__([RecordingNotifier()])

    def send_alerts(self, alerts: Any) -> Any:
        raise RuntimeError("notifier exploded")


class _FailingNotifier(Notifier):
    name = "failing"

    def send(self, notification: Any) -> NotificationReceipt:
        raise NotificationError("channel down")


class _FakeSMTP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], str]] = []
        self.closed = False

    def sendmail(self, from_addr: str, to_addrs: list[str], message: str) -> None:
        self.calls.append((from_addr, list(to_addrs), message))

    def close(self) -> None:
        self.closed = True


class _BrokenSMTP:
    def sendmail(self, from_addr: str, to_addrs: list[str], message: str) -> None:
        raise OSError("connection refused")


class TestNotificationService:
    """NotificationService + notifiers — Slack HTTP mocked, Email SMTP injected."""

    def _alert(self, title: str = "Salah injury") -> Alert:
        return Alert(
            alert_type=AlertType.INJURY,
            severity=AlertSeverity.HIGH,
            title=title,
            body="Hamstring injury",
            source_id="rss",
            created_at=NOW,
            url="https://example.com/salah",
        )

    def test_render_notification(self):
        notification = render_notification(self._alert(), "slack")
        assert notification.channel == "slack"
        assert "Salah injury" in notification.subject
        assert "[FPL Alert · injury]" in notification.subject
        assert "Hamstring injury" in notification.body
        assert "example.com/salah" in notification.body

    def test_slack_notifier_sends_payload(self):
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["json"] = request.read().decode("utf-8")
            return httpx.Response(200, json={"ok": True})

        notifier = SlackNotifier(
            "https://hooks.example.com/slack",
            http_client=_make_client(handler),
        )
        receipt = notifier.send(render_notification(self._alert(), "slack"))
        assert receipt.ok is True
        assert receipt.channel == "slack"
        assert captured["url"] == "https://hooks.example.com/slack"
        assert "Salah injury" in captured["json"]

    def test_slack_notifier_reports_http_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        notifier = SlackNotifier(
            "https://hooks.example.com/slack",
            http_client=_make_client(handler),
        )
        with pytest.raises(NotificationError):
            notifier.send(render_notification(self._alert(), "slack"))

    def test_slack_notifier_reports_network_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        notifier = SlackNotifier(
            "https://hooks.example.com/slack",
            http_client=_make_client(handler),
        )
        with pytest.raises(NotificationError):
            notifier.send(render_notification(self._alert(), "slack"))

    def test_email_notifier_sendmail_called(self):
        smtp = _FakeSMTP()
        notifier = EmailNotifier(
            "alerts@example.com",
            ["user@example.com"],
            smtp=smtp,
        )
        receipt = notifier.send(render_notification(self._alert(), "email"))
        assert receipt.ok is True
        assert len(smtp.calls) == 1
        from_addr, to_addrs, message = smtp.calls[0]
        assert from_addr == "alerts@example.com"
        assert to_addrs == ["user@example.com"]
        assert "Subject: [FPL Alert" in message

    def test_email_notifier_reports_sendmail_failure(self):
        notifier = EmailNotifier(
            "alerts@example.com",
            ["user@example.com"],
            smtp=_BrokenSMTP(),
        )
        with pytest.raises(NotificationError):
            notifier.send(render_notification(self._alert(), "email"))

    def test_log_notifier_sends(self):
        notifier = LogNotifier()
        receipt = notifier.send(render_notification(self._alert(), "log"))
        assert receipt.ok is True
        assert receipt.channel == "log"

    def test_recording_notifier_captures(self):
        notifier = RecordingNotifier()
        notifier.send(render_notification(self._alert(), "recording"))
        assert len(notifier.sent) == 1
        assert notifier.sent[0].subject.startswith("[FPL Alert")

    def test_service_dispatches_with_channel_isolation(self):
        recorder = RecordingNotifier()
        service = NotificationService([_FailingNotifier(), recorder])
        report = service.send_alerts([self._alert()])
        assert report.attempted == 2
        assert report.delivered == 1
        assert report.failed == 1
        assert not report.succeeded
        assert len(recorder.sent) == 1

    def test_service_batch_totals_and_to_dict(self):
        recorder = RecordingNotifier()
        service = NotificationService([recorder])
        report = service.send_alerts([self._alert("A injury"), self._alert("B injury")])
        assert report.alerts == 2
        assert report.attempted == 2
        assert report.delivered == 2
        assert report.succeeded
        d = report.to_dict()
        assert d["delivered"] == 2
        assert len(d["receipts"]) == 2

    def test_service_requires_at_least_one_notifier(self):
        with pytest.raises(ValueError):
            NotificationService([])

    def test_service_paces_sends_with_rate_limiter(self):
        record: list[float] = []
        service = NotificationService(
            [RecordingNotifier()],
            min_interval_seconds=5.0,
            monotonic_clock=lambda: 0.0,
            sleep=record.append,
        )
        service.send_alerts([self._alert("A injury"), self._alert("B injury")])
        # two sends; the first never waits, the second sleeps the min interval
        assert record == [5.0]
        assert service.rate_limiter.stats.calls == 2

    def test_service_caps_batch(self):
        service = NotificationService([RecordingNotifier()], max_alerts_per_batch=2)
        report = service.send_alerts(
            [self._alert("A injury"), self._alert("B injury"), self._alert("C injury")]
        )
        assert report.alerts == 2
        assert report.attempted == 2
