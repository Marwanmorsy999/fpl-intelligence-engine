"""Phase 9.6 — Notification Service.

Sends :class:`~fpl_intelligence.live_intelligence.scheduling.alerts.Alert`
objects to the user through one or more channels. Two production channels ship
out of the box:

* :class:`SlackNotifier` — POSTs the alert text to a Slack incoming-webhook URL
  over HTTP (:mod:`httpx`). No API token is hardcoded: the webhook URL is always
  supplied by the caller (CLI argument or environment variable).
* :class:`EmailNotifier` — sends via SMTP (:mod:`smtplib`, stdlib). Credentials
  are always supplied by the caller; an SMTP-like object can be injected as a
  test seam so no network connection is ever opened inside ``pytest``.
* :class:`LogNotifier` / :class:`RecordingNotifier` — local sinks useful for
  dry-runs and tests.

:class:`NotificationService` fans an alert out to every configured notifier,
pacing each send with the Phase 9.1 :class:`RateLimiter` and isolating failures
per channel: one broken channel is recorded on the dispatch report and never
aborts the remaining channels or the rest of the pipeline.

This module is additive: it does not modify the quantitative Phases 1–8 stack,
it makes **no** live API calls inside ``pytest`` (Slack HTTP is mocked with
``httpx.MockTransport``; Email SMTP is an injected fake), and it hardcodes no
credentials.
"""
from __future__ import annotations

import logging
import smtplib
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from fpl_intelligence.live_intelligence.rate_limit import (
    MonotonicClock,
    RateLimiter,
    SleepFn,
)
from fpl_intelligence.live_intelligence.scheduling.alerts import Alert
from fpl_intelligence.live_intelligence.temporal_ledger import Clock, utc_now


class NotificationError(RuntimeError):
    """Raised by a :class:`Notifier` when a send fails (caught by the service)."""


@dataclass(frozen=True)
class Notification:
    """A channel-ready rendering of one alert."""

    channel: str
    subject: str
    body: str
    alert: Alert

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "subject": self.subject,
            "body": self.body,
            "alert_type": self.alert.alert_type.value,
            "alert_title": self.alert.title,
        }


def render_notification(alert: Alert, channel: str) -> Notification:
    """Render an :class:`Alert` into a plain-text :class:`Notification`."""
    subject = f"[FPL Alert · {alert.alert_type.value}] {alert.title}"
    lines = [alert.body]
    if alert.player:
        lines.append(f"Player: {alert.player}")
    if alert.team:
        lines.append(f"Team: {alert.team}")
    if alert.url:
        lines.append(f"URL: {alert.url}")
    lines.append(f"Source: {alert.source_id} · severity: {alert.severity.value}")
    return Notification(channel=channel, subject=subject, body="\n".join(lines), alert=alert)


@dataclass
class NotificationReceipt:
    """Outcome of one send attempt on one channel."""

    channel: str
    title: str
    ok: bool
    error: str | None = None
    sent_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "title": self.title,
            "ok": self.ok,
            "error": self.error,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
        }


@dataclass
class NotificationDispatchReport:
    """Aggregate result of dispatching a batch of alerts."""

    alerts: int = 0
    attempted: int = 0
    delivered: int = 0
    failed: int = 0
    receipts: list[NotificationReceipt] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "alerts": self.alerts,
            "attempted": self.attempted,
            "delivered": self.delivered,
            "failed": self.failed,
            "succeeded": self.succeeded,
            "receipts": [r.to_dict() for r in self.receipts],
        }


class Notifier(ABC):
    """A single notification channel the service can dispatch to."""

    #: Stable channel key (used for receipts and de-duplication).
    name: str = "base"

    @abstractmethod
    def send(self, notification: Notification) -> NotificationReceipt:
        """Send one notification.

        Returns a receipt on success; raises :class:`NotificationError` on
        failure. The service converts a raised error into a failed receipt so a
        broken channel can never propagate.
        """

    def close(self) -> None:
        """Release any held resources. Base implementation is a no-op."""
        return None


class LogNotifier(Notifier):
    """Write notifications to a logger (safe local sink / dry-run channel)."""

    name = "log"

    def __init__(
        self, *, logger: logging.Logger | None = None, clock: Clock = utc_now
    ) -> None:
        self._logger = logger or logging.getLogger("fpl_intelligence.notifications")
        self._clock = clock

    def send(self, notification: Notification) -> NotificationReceipt:
        message = f"{notification.channel} | {notification.subject}\n{notification.body}"
        self._logger.info(message)
        return NotificationReceipt(
            channel=self.name, title=notification.subject, ok=True, sent_at=self._clock()
        )


class RecordingNotifier(Notifier):
    """Capture notifications in memory. Used by tests and dry-run tooling."""

    name = "recording"

    def __init__(self, *, clock: Clock = utc_now) -> None:
        self._clock = clock
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> NotificationReceipt:
        self.sent.append(notification)
        return NotificationReceipt(
            channel=self.name, title=notification.subject, ok=True, sent_at=self._clock()
        )


class SlackNotifier(Notifier):
    """Post alerts to a Slack incoming-webhook URL over HTTP.

    The webhook URL is never hardcoded here: it comes from the constructor
    (the CLI reads it from ``--slack-webhook-url`` or ``SLACK_WEBHOOK_URL``).
    Tests inject an ``httpx.Client`` backed by ``httpx.MockTransport`` so no
    live network call is ever made inside ``pytest``.
    """

    name = "slack"

    def __init__(
        self,
        webhook_url: str,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        clock: Clock = utc_now,
    ) -> None:
        if not webhook_url:
            raise ValueError("webhook_url must not be empty")
        self._webhook_url = webhook_url
        self._timeout = timeout
        self._clock = clock
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client()

    @property
    def webhook_url(self) -> str:
        return self._webhook_url

    def send(self, notification: Notification) -> NotificationReceipt:
        payload = {"text": f"*{notification.subject}*\n{notification.body}"}
        try:
            response = self._client.post(self._webhook_url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise NotificationError(f"slack webhook POST failed: {exc}") from exc
        if response.is_error:
            raise NotificationError(f"slack webhook -> HTTP {response.status_code}")
        return NotificationReceipt(
            channel=self.name, title=notification.subject, ok=True, sent_at=self._clock()
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class EmailNotifier(Notifier):
    """Send alerts by email over SMTP.

    Credentials / recipients come from the constructor only (the CLI reads
    them from arguments or environment variables — never from this module).
    For tests an SMTP-compatible object (anything with a ``sendmail`` method)
    can be injected via ``smtp`` so no socket is ever opened inside ``pytest``.
    """

    name = "email"

    def __init__(
        self,
        from_addr: str,
        to_addrs: Iterable[str],
        *,
        host: str = "localhost",
        port: int = 25,
        username: str | None = None,
        password: str | None = None,
        smtp: Any | None = None,
        timeout: float = 30.0,
        clock: Clock = utc_now,
    ) -> None:
        if not from_addr:
            raise ValueError("from_addr must not be empty")
        recipients = [a.strip() for a in to_addrs if a.strip()]
        if not recipients:
            raise ValueError("at least one recipient is required")
        self._from_addr = from_addr
        self._to_addrs = recipients
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._smtp = smtp
        self._timeout = timeout
        self._clock = clock

    @property
    def recipients(self) -> list[str]:
        return list(self._to_addrs)

    def _render_message(self, notification: Notification) -> str:
        headers = (
            f"From: {self._from_addr}\r\n"
            f"To: {', '.join(self._to_addrs)}\r\n"
            f"Subject: {notification.subject}\r\n\r\n"
        )
        return headers + notification.body

    def send(self, notification: Notification) -> NotificationReceipt:
        message = self._render_message(notification)
        try:
            if self._smtp is not None:
                self._smtp.sendmail(self._from_addr, self._to_addrs, message)
            else:
                with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as server:
                    if self._username:
                        server.starttls()
                        server.login(self._username, self._password or "")
                    server.sendmail(self._from_addr, self._to_addrs, message)
        except OSError as exc:
            # smtplib.SMTPException subclasses OSError, so this single branch
            # covers connection, login and sendmail failures.
            raise NotificationError(f"email send failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - injected smtp seam may raise anything
            raise NotificationError(f"email send failed: {exc}") from exc
        return NotificationReceipt(
            channel=self.name, title=notification.subject, ok=True, sent_at=self._clock()
        )

    def close(self) -> None:
        if self._smtp is not None and hasattr(self._smtp, "close"):
            self._smtp.close()


class NotificationService:
    """Fan alerts out to every configured notifier, rate-limited and isolated.

    Args:
        notifiers: The channels to dispatch to. At least one is required.
        clock: Wall clock stamping receipts (injectable for tests).
        monotonic_clock / sleep: Seams backing the internal :class:`RateLimiter`.
        min_interval_seconds: Minimum gap between successive sends (``0``
            disables pacing) — protects webhooks / SMTP servers from bursts.
        max_alerts_per_batch: Hard ceiling on alerts dispatched per batch call.
    """

    def __init__(
        self,
        notifiers: Iterable[Notifier],
        *,
        clock: Clock = utc_now,
        monotonic_clock: MonotonicClock = time.monotonic,
        sleep: SleepFn = time.sleep,
        min_interval_seconds: float = 0.0,
        max_alerts_per_batch: int = 100,
    ) -> None:
        self._notifiers = {n.name: n for n in notifiers}
        if not self._notifiers:
            raise ValueError("at least one notifier is required")
        self._clock = clock
        self._rate = RateLimiter(
            min_interval_seconds, clock=monotonic_clock, sleep=sleep
        )
        self._max_alerts_per_batch = int(max_alerts_per_batch)

    @property
    def notifiers(self) -> Mapping[str, Notifier]:
        return dict(self._notifiers)

    @property
    def rate_limiter(self) -> RateLimiter:
        """Expose pacing so callers / tests can inspect the rate-limit stats."""
        return self._rate

    def send_alert(self, alert: Alert) -> NotificationDispatchReport:
        """Dispatch a single alert to every channel."""
        return self.send_alerts([alert])

    def send_alerts(
        self, alerts: Iterable[Alert],
    ) -> NotificationDispatchReport:
        """Dispatch a batch of alerts to every channel.

        Every send acquires the :class:`RateLimiter` first. A failure on one
        channel becomes a failed receipt; the other channels (and the rest of
        the batch) are unaffected. ``max_alerts_per_batch`` caps the batch so a
        runaway alert flood can never hammer the user's channels.
        """
        report = NotificationDispatchReport()
        for alert in alerts:
            if report.alerts >= self._max_alerts_per_batch:
                break
            report.alerts += 1
            for notifier in self._notifiers.values():
                self._rate.acquire()
                notification = render_notification(alert, notifier.name)
                report.attempted += 1
                try:
                    receipt = notifier.send(notification)
                except Exception as exc:  # noqa: BLE001 isolate per-channel failures
                    receipt = NotificationReceipt(
                        channel=notifier.name,
                        title=alert.title,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        sent_at=self._clock(),
                    )
                report.receipts.append(receipt)
                if receipt.ok:
                    report.delivered += 1
                else:
                    report.failed += 1
        return report

    def close(self) -> None:
        """Close every channel, swallowing per-channel close errors."""
        for notifier in self._notifiers.values():
            try:
                notifier.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                continue