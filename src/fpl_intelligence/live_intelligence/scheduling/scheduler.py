"""Phase 9.6 — Scheduler.

The top of the scheduling stack: a :class:`Scheduler` wraps a Phase 9.5
:class:`~fpl_intelligence.live_intelligence.connectors.scheduler.ConnectorScheduler`
and runs the full pipeline per pass —

    fetch (connectors) → ingest (Phase 9.2) → alert (AlertGenerator) → notify (NotificationService)

— on demand (:meth:`run`) or on a schedule (:meth:`run_scheduled`).

Each stage is optional and error-isolated:

* fetch errors from a connector are contained by the underlying
  ``ConnectorScheduler`` (recorded per-connector, never aborting the run);
* a failing ingestion sink for one item is likewise recorded per-connector;
* alert-generation and notification failures are captured on the run report
  without aborting the pass;
* a ``RateLimiter`` paces successive passes (a hard floor on the gap between
  them) so a tight scheduled loop cannot hammer public feeds or webhooks.

The ingestion sink is injected as a callable ``(raw, *, connector, dry_run)``;
the CLI wires it to the Phase 9.2 ``ingest_raw_text`` pipeline, and tests wire
it to a recorder. The Scheduler never touches the database itself, so it stays
offline-testable and DB-agnostic.

This module is additive: it does not modify the quantitative Phases 1–8 stack,
it makes **no** live API calls inside ``pytest`` (connectors inject mocked HTTP
transports), and it hardcodes no keys.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fpl_intelligence.live_intelligence.connectors.base import SourceConnector
from fpl_intelligence.live_intelligence.connectors.scheduler import (
    ConnectorMap,
    ConnectorScheduler,
    SchedulerReport,
)
from fpl_intelligence.live_intelligence.rate_limit import (
    MonotonicClock,
    RateLimiter,
)
from fpl_intelligence.live_intelligence.raw_item_ledger import RawItem
from fpl_intelligence.live_intelligence.scheduling.alerts import (
    Alert,
    AlertGenerator,
)
from fpl_intelligence.live_intelligence.scheduling.notification import (
    NotificationDispatchReport,
    NotificationService,
)

#: The ingestion sink: accept one raw item and return an (opaque) report. The
#: CLI passes a closure over the Phase 9.2 ``ingest_raw_text`` pipeline. Like
#: the Phase 9.5 ``IngestionSink``, it is invoked with keyword args
#: (``connector=`` / ``dry_run=``), hence the permissive callable type.
type IngestFn = Callable[..., Any]


@dataclass
class SchedulerRunReport:
    """Everything one pass of the scheduler produced."""

    connector_report: SchedulerReport
    ingested_items: list[RawItem] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    notifications: NotificationDispatchReport | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.connector_report.succeeded and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector_report": self.connector_report.to_dict(),
            "ingested_items": len(self.ingested_items),
            "alerts": [a.to_dict() for a in self.alerts],
            "notifications": (
                self.notifications.to_dict() if self.notifications is not None else None
            ),
            "errors": list(self.errors),
        }


class Scheduler:
    """Orchestrate fetch → ingest → alert → notify on demand or on a schedule.

    Args:
        connectors: Map of connector name → :class:`SourceConnector`.
        ingest: Sink called for every fetched item:
            ``(raw, *, connector, dry_run) -> report``. The report is opaque
            to the scheduler; in the CLI it is the Phase 9.2 ``ingest_raw_text``
            wrapper, and in tests it is a recorder.
        alert_generator: Optional :class:`AlertGenerator`; enables the alert stage.
        notification_service: Optional :class:`NotificationService`; enables the
            notification stage.
        min_interval_seconds: Hard floor on the gap between successive passes
            (``0`` disables pacing). The effective cadence of
            :meth:`run_scheduled` is
            ``max(interval_seconds, min_interval_seconds)``.
        monotonic_clock / sleep: Backing the internal :class:`RateLimiter`
            and the scheduled loop (injectable for offline tests).
    """

    def __init__(
        self,
        connectors: ConnectorMap,
        *,
        ingest: IngestFn,
        alert_generator: AlertGenerator | None = None,
        notification_service: NotificationService | None = None,
        min_interval_seconds: float = 0.0,
        monotonic_clock: MonotonicClock = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ingest = ingest
        self._alert_generator = alert_generator
        self._notification_service = notification_service
        self._sleep = sleep
        self._rate = RateLimiter(min_interval_seconds, clock=monotonic_clock, sleep=sleep)
        self._buffer: list[RawItem] = []
        self._connector_scheduler = ConnectorScheduler(connectors, self._sink, sleep=sleep)

    @property
    def connectors(self) -> Mapping[str, SourceConnector]:
        return self._connector_scheduler.connectors

    @property
    def rate_limiter(self) -> RateLimiter:
        """Expose pacing so callers/tests can inspect the rate-limit stats."""
        return self._rate

    def _sink(self, raw: RawItem, *, connector: SourceConnector, dry_run: bool) -> Any:
        report = self._ingest(raw, connector=connector, dry_run=dry_run)
        self._buffer.append(raw)
        return report

    # -- manual trigger ---------------------------------------------------

    def run(
        self,
        *,
        connector: str | None = None,
        dry_run: bool = False,
        generate_alerts: bool = True,
        notify: bool = True,
    ) -> SchedulerRunReport:
        """Run one pass over the requested connector(s) and return a report.

        ``dry_run`` is forwarded to the ingestion sink so the pipeline can fetch
        without persisting. The alert and notify stages run over the items the sink
        accepted. A failure in either stage is captured on the report, never raised.
        """
        self._rate.acquire()
        self._buffer.clear()
        connector_report = self._connector_scheduler.run(connector=connector, dry_run=dry_run)
        ingested_items = list(self._buffer)
        report = SchedulerRunReport(
            connector_report=connector_report,
            ingested_items=ingested_items,
        )

        if generate_alerts and self._alert_generator is not None and ingested_items:
            try:
                generation = self._alert_generator.generate(ingested_items)
                report.alerts = list(generation.alerts)
                report.errors.extend(generation.errors)
            except Exception as exc:  # noqa: BLE001 - never abort the pass
                report.errors.append(f"alert generation failed: {exc}")

        if notify and self._notification_service is not None and report.alerts:
            try:
                report.notifications = self._notification_service.send_alerts(report.alerts)
            except Exception as exc:  # noqa: BLE001 - never abort the pass
                report.errors.append(f"notification dispatch failed: {exc}")

        return report

    # -- scheduled execution -------------------------------------------------

    def run_scheduled(
        self,
        *,
        interval_seconds: float,
        iterations: int = 1,
        connector: str | None = None,
        dry_run: bool = False,
        stop_event: Any | None = None,
    ) -> list[SchedulerRunReport]:
        """Run :meth:`run` in a loop, returning one report per pass.

        ``iterations`` bounds the loop (``0`` / negative means run until
        ``stop_event`` is set, which tests never enter). ``stop_event``
        (e.g. ``threading.Event``) is checked before every pass and before the
        sleep between passes, for graceful shutdown.
        """
        reports: list[SchedulerRunReport] = []
        passes = 0
        while True:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            reports.append(self.run(connector=connector, dry_run=dry_run))
            passes += 1
            if iterations and passes >= iterations:
                break
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            self._sleep(max(0.0, float(interval_seconds)))
        return reports
