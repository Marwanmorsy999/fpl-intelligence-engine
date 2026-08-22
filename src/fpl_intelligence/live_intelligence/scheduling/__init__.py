"""Phase 9.6 — Scheduling and Alerting.

Automates live ingestion on a schedule and turns the ingested news into
user-facing alerts and notifications.

* :class:`~fpl_intelligence.live_intelligence.scheduling.scheduler.Scheduler` —
  orchestrates fetch (Phase 9.5 connectors) → ingest (Phase 9.2) → alert →
  notify, on demand (:meth:`~Scheduler.run`) or on a schedule
  (:meth:`~Scheduler.run_scheduled`). Manual triggering, rate limiting and
  error isolation are built in.
* :class:`~fpl_intelligence.live_intelligence.scheduling.alerts.AlertGenerator`
  — classifies raw items into :class:`Alert` objects (injury, availability
  risk, tactical change, transfer news, general) with local heuristics; no
  network calls, rate-limited passes, per-item error isolation.
* :class:`~fpl_intelligence.live_intelligence.scheduling.notification.NotificationService`
  — fans alerts out to one or more :class:`Notifier` channels (Slack webhook,
  SMTP email, log, recording) with rate limiting and per-channel error
  isolation.
* `scripts/run_scheduler.py` — the Phase 9.6 CLI (``--connector``, ``--dry-run``).

This layer is additive: it does **not** modify the quantitative Phases 1–8
stack, makes **no** live API calls inside ``pytest`` (all HTTP is mocked with
``httpx.MockTransport`` / injected SMTP seams), hardcodes no API keys, and
performs no aggressive scraping.
"""

from __future__ import annotations

from fpl_intelligence.live_intelligence.scheduling.alerts import (
    DEFAULT_KEYWORDS,
    DEFAULT_SEVERITY,
    Alert,
    AlertGenerationReport,
    AlertGenerator,
    AlertSeverity,
    AlertType,
    classify_alert_type,
)
from fpl_intelligence.live_intelligence.scheduling.notification import (
    EmailNotifier,
    LogNotifier,
    Notification,
    NotificationDispatchReport,
    NotificationError,
    NotificationReceipt,
    NotificationService,
    Notifier,
    RecordingNotifier,
    SlackNotifier,
    render_notification,
)
from fpl_intelligence.live_intelligence.scheduling.scheduler import (
    Scheduler,
    SchedulerRunReport,
)

__all__ = [
    "Alert",
    "AlertGenerationReport",
    "AlertGenerator",
    "AlertSeverity",
    "AlertType",
    "DEFAULT_KEYWORDS",
    "DEFAULT_SEVERITY",
    "EmailNotifier",
    "LogNotifier",
    "Notification",
    "NotificationDispatchReport",
    "NotificationError",
    "NotificationReceipt",
    "NotificationService",
    "Notifier",
    "RecordingNotifier",
    "Scheduler",
    "SchedulerRunReport",
    "SlackNotifier",
    "classify_alert_type",
    "render_notification",
]
