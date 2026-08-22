"""Phase 9.8 — Monitoring and Logging.

Operational observability for the deployed system. This is deliberately
distinct from the Phase 9.6 user-facing alerting (news alerts): here the
alerts are **operational** — critical errors, breached metric thresholds and
failing health checks.

Components:

* :class:`MetricRegistry` — counters and gauges with a ``snapshot()`` for
  exporters and dashboards.
* :class:`HealthRegistry` — per-component health checks and an aggregate
  ``ok``/``degraded``/``down`` summary.
* :class:`AlertManager` + :class:`AlertRule` — threshold rules over the metric
  registry, delivered to :class:`AlertSink` channels with cooldown dedup so a
  persistent failure alerts once, not every poll. Sinks: :class:`LogAlertSink`
  (application log) and :class:`WebhookAlertSink` (HTTP POST, injectable
  ``httpx`` client so tests never touch the network).
* :class:`ProductionJsonFormatter` / :func:`setup_production_logging` — one-line
  JSON log records for machine consumption.
* :class:`MonitoringService` — the bundle the deployment runner wires up, plus
  :func:`build_monitoring_service` which maps a :class:`ProductionConfig` onto
  monitoring components.

This module is additive: it does not modify the quantitative Phases 1–8 stack,
it makes **no** live API calls inside ``pytest`` (webhook HTTP is mocked with
``httpx.MockTransport``; metrics and logs are asserted in-memory), and it
hardcodes no credentials (the webhook URL always comes from configuration /
environment).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

import httpx

from fpl_intelligence.live_intelligence.rate_limit import MonotonicClock
from fpl_intelligence.live_intelligence.temporal_ledger import Clock, utc_now


class MetricKind(StrEnum):
    """The two supported metric families."""

    COUNTER = "counter"
    GAUGE = "gauge"


@dataclass
class Metric:
    """One named metric with its current value and provenance."""

    name: str
    kind: MetricKind
    value: float = 0.0
    unit: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "value": self.value,
            "unit": self.unit,
            "labels": dict(self.labels),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class MetricRegistry:
    """Thread-local-in-practice registry of counters and gauges."""

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._metrics: dict[str, Metric] = {}

    def register(
        self,
        name: str,
        kind: MetricKind,
        *,
        unit: str = "",
        labels: dict[str, str] | None = None,
    ) -> Metric:
        """Register a metric, or return the existing one (kind must match)."""
        existing = self._metrics.get(name)
        if existing is not None:
            if existing.kind is not kind:
                raise ValueError(
                    f"metric {name!r} already registered as {existing.kind.value}, "
                    f"cannot re-register as {kind.value}"
                )
            return existing
        metric = Metric(
            name=name,
            kind=kind,
            unit=unit,
            labels=dict(labels or {}),
            updated_at=self._clock(),
        )
        self._metrics[name] = metric
        return metric

    def counter(
        self,
        name: str,
        *,
        unit: str = "",
        labels: dict[str, str] | None = None,
    ) -> Metric:
        return self.register(name, MetricKind.COUNTER, unit=unit, labels=labels)

    def gauge(
        self,
        name: str,
        *,
        unit: str = "",
        labels: dict[str, str] | None = None,
    ) -> Metric:
        return self.register(name, MetricKind.GAUGE, unit=unit, labels=labels)

    def increment(self, name: str, amount: float = 1.0) -> float:
        """Add ``amount`` to a counter (auto-registered on first use)."""
        if amount <= 0:
            raise ValueError("increment amount must be positive")
        metric = self.counter(name)
        metric.value += amount
        metric.updated_at = self._clock()
        return metric.value

    def set(self, name: str, value: float) -> float:
        """Set a gauge (auto-registered on first use)."""
        metric = self.gauge(name)
        metric.value = float(value)
        metric.updated_at = self._clock()
        return metric.value

    def get(self, name: str) -> Metric | None:
        return self._metrics.get(name)

    def snapshot(self) -> dict[str, Metric]:
        """A shallow copy of all registered metrics (safe to iterate)."""
        return dict(self._metrics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": [metric.to_dict() for metric in self._metrics.values()],
        }


class HealthStatus(StrEnum):
    """Aggregate health of one component or of the whole system."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass
class HealthCheck:
    """The latest probe result for one component."""

    name: str
    ok: bool
    detail: str = ""
    checked_at: datetime | None = None

    @property
    def status(self) -> HealthStatus:
        return HealthStatus.OK if self.ok else HealthStatus.DOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status.value,
            "detail": self.detail,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }


class HealthRegistry:
    """Latest health probe per component, plus the aggregate summary."""

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._checks: dict[str, HealthCheck] = {}

    def report(self, name: str, ok: bool, detail: str = "") -> HealthCheck:
        """Record the latest probe result for a component."""
        check = HealthCheck(
            name=name,
            ok=bool(ok),
            detail=detail,
            checked_at=self._clock(),
        )
        self._checks[name] = check
        return check

    def get(self, name: str) -> HealthCheck | None:
        return self._checks.get(name)

    def snapshot(self) -> dict[str, HealthCheck]:
        return dict(self._checks)

    def all_ok(self) -> bool:
        """True when every registered component passed its latest probe."""
        return all(check.ok for check in self._checks.values())

    def summary(self) -> str:
        """e.g. ``ok=3 degraded=0 down=1`` — for logs and readiness reports."""
        counts: dict[str, int] = {"ok": 0, "degraded": 0, "down": 0}
        for check in self._checks.values():
            counts[check.status.value] += 1
        return f"ok={counts['ok']} degraded={counts['degraded']} down={counts['down']}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_ok": self.all_ok(),
            "summary": self.summary(),
            "checks": [check.to_dict() for check in self._checks.values()],
        }


class AlertDeliveryError(RuntimeError):
    """Raised when a sink cannot deliver an alert (caught by the manager)."""


@dataclass(frozen=True)
class AlertRule:
    """Fire an alert when ``metric`` breaches ``threshold`` in ``direction``."""

    name: str
    metric: str
    threshold: float
    direction: str = "above"
    severity: str = "critical"
    message: str = ""

    def __post_init__(self) -> None:
        if self.direction not in ("above", "below"):
            raise ValueError("direction must be 'above' or 'below'")

    def breached(self, value: float) -> bool:
        if self.direction == "above":
            return value >= self.threshold
        return value <= self.threshold


@dataclass
class Alert:
    """One fired operational alert."""

    rule: str
    severity: str
    message: str
    metric_value: float
    fired_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "metric_value": self.metric_value,
            "fired_at": self.fired_at.isoformat() if self.fired_at else None,
        }


class AlertSink(Protocol):
    """A channel operational alerts are delivered to."""

    def send(self, alert: Alert) -> None: ...

    def close(self) -> None: ...


class LogAlertSink:
    """Write alerts to the application log at CRITICAL level (always available)."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("fpl_intelligence.deployment.alerts")

    def send(self, alert: Alert) -> None:
        self._logger.critical(
            "[alert:%s] %s (metric=%s value=%s)",
            alert.severity,
            alert.message,
            alert.rule,
            alert.metric_value,
        )

    def close(self) -> None:
        return None


class WebhookAlertSink:
    """POST alerts to an operational webhook (e.g. a health-incident channel).

    The ``httpx`` client is injectable; tests use ``httpx.MockTransport`` so no
    network connection is ever opened inside ``pytest``.
    """

    def __init__(
        self,
        url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._url = url
        self._client = client if client is not None else httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def send(self, alert: Alert) -> None:
        payload = alert.to_dict()
        try:
            response = self._client.post(self._url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AlertDeliveryError(f"webhook alert delivery failed: {exc}") from exc

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class AlertManager:
    """Evaluate threshold rules over a metric registry and fan alerts out to sinks.

    A rule whose metric is absent does not fire (not yet observed). A rule that stays
    breached re-fires only after ``cooldown_seconds``, so a persistent condition alerts once
    per interval instead of spamming the channel every poll.
    """

    def __init__(
        self,
        rules: Iterable[AlertRule],
        sinks: Iterable[AlertSink],
        *,
        clock: Clock = utc_now,
        monotonic_clock: MonotonicClock = time.monotonic,
        cooldown_seconds: float = 60.0,
    ) -> None:
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        self._rules = list(rules)
        self._sinks = list(sinks)
        self._clock = clock
        self._monotonic = monotonic_clock
        self._cooldown_seconds = float(cooldown_seconds)
        self._last_fired: dict[str, float] = {}
        self._fired_count: int = 0
        self._errors: list[str] = []

    @property
    def rules(self) -> list[AlertRule]:
        return list(self._rules)

    @property
    def sinks(self) -> list[AlertSink]:
        return list(self._sinks)

    @property
    def cooldown_seconds(self) -> float:
        return self._cooldown_seconds

    @property
    def fired_count(self) -> int:
        return self._fired_count

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def evaluate(self, registry: MetricRegistry) -> list[Alert]:
        """Check every rule against the registry and fire the breached ones."""
        fired: list[Alert] = []
        now_mono = self._monotonic()
        for rule in self._rules:
            metric = registry.get(rule.metric)
            if metric is None:
                continue  # not observed yet — do not alert on an absent metric
            if not rule.breached(metric.value):
                continue
            last = self._last_fired.get(rule.name)
            if last is not None and now_mono - last < self._cooldown_seconds:
                continue  # inside the cooldown window
            message = rule.message or (
                f"metric {rule.metric} breached threshold "
                f"({metric.value} {rule.direction} {rule.threshold})"
            )
            alert = Alert(
                rule=rule.name,
                severity=rule.severity,
                message=message,
                metric_value=metric.value,
                fired_at=self._clock(),
            )
            self.fire(alert)
            self._last_fired[rule.name] = now_mono
            fired.append(alert)
        return fired

    def fire(self, alert: Alert) -> None:
        """Deliver one alert to every sink, isolating per-sink failures."""
        for sink in self._sinks:
            try:
                sink.send(alert)
            except Exception as exc:  # noqa: BLE001 - one bad sink must not block the others
                self._errors.append(f"{type(sink).__name__}: {exc}")
        self._fired_count += 1

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                continue

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": [rule.name for rule in self._rules],
            "sinks": [type(sink).__name__ for sink in self._sinks],
            "cooldown_seconds": self._cooldown_seconds,
            "fired_count": self._fired_count,
            "errors": list(self._errors),
        }


class ProductionJsonFormatter(logging.Formatter):
    """Render log records as single-line JSON for machine consumption.

    An ``extra`` dict attached to a record as ``extra_fields`` is merged into the
    payload so callers can attach structured context (operation ids, etc.).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


def setup_production_logging(
    level: str = "INFO",
    *,
    logger_name: str = "fpl_intelligence",
    json_format: bool = False,
) -> logging.Logger:
    """Configure and return the application logger for production.

    Configures the ``fpl_intelligence`` logger tree (not the root logger), so it
    composes cleanly with the existing Phase 9 logging and with pytest's caplog.
    ``json_format`` switches the stream handler to :class:`ProductionJsonFormatter`.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        handler = logging.StreamHandler()
        if json_format:
            handler.setFormatter(ProductionJsonFormatter())
        else:
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
        logger.addHandler(handler)
    logger.propagate = True
    return logger


@dataclass
class MonitoringConfig:
    """Wire-up for a :class:`MonitoringService`."""

    metrics_enabled: bool = True
    alert_rules: tuple[AlertRule, ...] = ()
    alert_sinks: tuple[AlertSink, ...] = ()
    alert_cooldown_seconds: float = 60.0
    health_check_interval_seconds: float = 60.0


class MonitoringService:
    """The bundle the deployment runner wires up: metrics + health + alerts.

    ``record_metric`` / ``report_health`` are the two calls the running pipeline
    makes; ``check_alerts`` sweeps the threshold rules; ``report_critical_error``
    fires an immediate operational alert for an unhandled failure.
    """

    def __init__(
        self,
        *,
        registry: MetricRegistry | None = None,
        health: HealthRegistry | None = None,
        alert_manager: AlertManager | None = None,
        config: MonitoringConfig | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._config = config or MonitoringConfig()
        self._clock = clock
        self._registry = registry or MetricRegistry(clock=clock)
        self._health = health or HealthRegistry(clock=clock)
        if alert_manager is None and self._config.alert_rules:
            alert_manager = AlertManager(
                self._config.alert_rules,
                self._config.alert_sinks,
                clock=clock,
                cooldown_seconds=self._config.alert_cooldown_seconds,
            )
        self._alert_manager = alert_manager

    @property
    def metrics(self) -> MetricRegistry:
        return self._registry

    @property
    def health(self) -> HealthRegistry:
        return self._health

    @property
    def alerts(self) -> AlertManager | None:
        return self._alert_manager

    @property
    def config(self) -> MonitoringConfig:
        return self._config

    def record_metric(
        self,
        name: str,
        value: float,
        *,
        kind: MetricKind = MetricKind.GAUGE,
    ) -> float:
        """Record a gauge (``set``) or counter (``increment``) value."""
        if not self._config.metrics_enabled:
            return value
        if kind is MetricKind.COUNTER:
            return self._registry.increment(name, value)
        return self._registry.set(name, value)

    def report_health(self, name: str, ok: bool, detail: str = "") -> HealthCheck:
        return self._health.report(name, ok, detail)

    def check_alerts(self) -> list[Alert]:
        """Sweep threshold rules; returns the alerts fired in this sweep."""
        if self._alert_manager is None or not self._config.metrics_enabled:
            return []
        return self._alert_manager.evaluate(self._registry)

    def report_critical_error(self, message: str) -> None:
        """Fire an immediate critical alert (best-effort, never raises)."""
        if self._alert_manager is None:
            return
        alert = Alert(
            rule="critical_error",
            severity="critical",
            message=message,
            metric_value=0.0,
            fired_at=self._clock(),
        )
        self._alert_manager.fire(alert)

    def snapshot(self) -> dict[str, Any]:
        return {
            "metrics": self._registry.to_dict(),
            "health": self._health.to_dict(),
            "alerts_fired": self._alert_manager.fired_count if self._alert_manager else 0,
        }

    def close(self) -> None:
        if self._alert_manager is not None:
            self._alert_manager.close()


def build_monitoring_service(
    config: Any,
    *,
    webhook_client: httpx.Client | None = None,
) -> MonitoringService:
    """Map a :class:`~fpl_intelligence.deployment.config.ProductionConfig` onto a
    monitoring service.

    The log sink is always present. A ``critical_error_webhook_url`` configures
    an additional webhook sink (the client seam lets tests mock the HTTP). The
    shipped rules watch the operational counters the pipeline maintains
    (``ingest_failures_total``, ``scheduler_errors_total``, ``health_checks_failed``).
    """
    sinks: list[AlertSink] = [LogAlertSink()]
    if getattr(config, "critical_error_webhook_url", None):
        sinks.append(
            WebhookAlertSink(
                config.critical_error_webhook_url,
                client=webhook_client,
            )
        )
    rules = (
        AlertRule(
            name="health_all_ok",
            metric="health_checks_failed",
            threshold=1.0,
            direction="above",
            severity="critical",
            message="one or more health checks are failing",
        ),
        AlertRule(
            name="ingest_failures",
            metric="ingest_failures_total",
            threshold=5.0,
            direction="above",
            severity="warning",
            message="ingestion failures exceeded threshold",
        ),
        AlertRule(
            name="scheduler_errors",
            metric="scheduler_errors_total",
            threshold=10.0,
            direction="above",
            severity="critical",
            message="scheduler errors exceeded threshold",
        ),
    )
    monitoring_config = MonitoringConfig(
        metrics_enabled=bool(getattr(config, "metrics_enabled", True)),
        alert_rules=rules,
        alert_sinks=tuple(sinks),
        alert_cooldown_seconds=max(
            0.0, float(getattr(config, "health_check_interval_seconds", 60.0))
        ),
        health_check_interval_seconds=float(getattr(config, "health_check_interval_seconds", 60.0)),
    )
    return MonitoringService(config=monitoring_config)
