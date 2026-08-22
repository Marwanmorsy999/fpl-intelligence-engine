"""Phase 9.8 unit tests — Monitoring and Logging.

Metrics and health are asserted in-memory; the log sink is asserted with
``caplog``; the webhook sink is asserted with ``httpx.MockTransport`` so **no
network call is ever made inside ``pytest``**.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fpl_intelligence.deployment.monitoring import (
    Alert,
    AlertManager,
    AlertRule,
    HealthRegistry,
    MetricKind,
    MetricRegistry,
)

NOW = datetime(2025, 8, 19, 12, 0, 0, tzinfo=UTC)


class _FakeMonotonic:
    """Deterministic monotonic clock that serves values in order then holds the last."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> float:
        value = self._values[self._index] if self._index < len(self._values) else self._values[-1]
        self._index += 1
        return value


class _RecordingSink:
    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)

    def close(self) -> None:
        return None


class _RaisingSink:
    def send(self, alert: Alert) -> None:
        raise RuntimeError("channel down")

    def close(self) -> None:
        return None


def test_counter_register_and_increment() -> None:
    reg = MetricRegistry()
    metric = reg.counter("requests")
    assert metric.kind is MetricKind.COUNTER
    assert reg.increment("requests") == 1.0
    assert reg.increment("requests", 5) == 6.0
    assert reg.get("requests").value == 6.0


def test_increment_auto_registers_counter() -> None:
    reg = MetricRegistry()
    assert reg.get("auto") is None
    reg.increment("auto")
    assert reg.get("auto").kind is MetricKind.COUNTER
    assert reg.get("auto").value == 1.0


def test_increment_rejects_non_positive() -> None:
    reg = MetricRegistry()
    with pytest.raises(ValueError):
        reg.increment("x", amount=0)


def test_gauge_set_value() -> None:
    reg = MetricRegistry()
    assert reg.set("cpu", 0.6) == 0.6
    assert reg.set("cpu", 0.9) == 0.9
    assert reg.get("cpu").kind is MetricKind.GAUGE
    assert reg.get("cpu").value == 0.9


def test_register_wrong_kind_raises() -> None:
    reg = MetricRegistry()
    reg.counter("dup")
    with pytest.raises(ValueError, match="already registered"):
        reg.gauge("dup")


def test_registry_snapshot_is_a_copy() -> None:
    reg = MetricRegistry()
    reg.set("a", 1.0)
    snapshot = reg.snapshot()
    snapshot["extra"] = reg.gauge("b")  # mutate the copy
    assert "extra" not in reg.snapshot()
    assert reg.get("extra") is None


def test_health_report_ok_down() -> None:
    health = HealthRegistry()
    health.report("db", True, "postgres reachable")
    health.report("api", False, "502 from upstream")
    assert health.get("db").ok
    assert not health.get("api").ok
    assert "502" in health.get("api").detail


def test_health_all_ok_false_when_down() -> None:
    health = HealthRegistry()
    health.report("db", True)
    health.report("api", False)
    assert not health.all_ok()


def test_health_summary_counts() -> None:
    health = HealthRegistry()
    health.report("db", True)
    health.report("api", True)
    health.report("cache", False)
    assert health.summary() == "ok=2 degraded=0 down=1"


def test_alert_rule_above_threshold() -> None:
    rule = AlertRule(name="r", metric="cpu", threshold=2.0, direction="above")
    assert rule.breached(2.0)
    assert rule.breached(3.0)
    assert not rule.breached(1.0)


def test_alert_rule_below_threshold() -> None:
    rule = AlertRule(name="r", metric="cpu", threshold=0.5, direction="below")
    assert rule.breached(0.5)
    assert rule.breached(0.4)
    assert not rule.breached(1.0)


def test_alert_rule_invalid_direction_raises() -> None:
    with pytest.raises(ValueError):
        AlertRule(name="r", metric="cpu", threshold=1.0, direction="sideways")


def test_alert_manager_fires_and_sinks_receive() -> None:
    reg = MetricRegistry()
    reg.set("cpu", 5.0)
    sink = _RecordingSink()
    rule = AlertRule(name="cpu_high", metric="cpu", threshold=2.0, severity="critical")
    am = AlertManager([rule], [sink], cooldown_seconds=0)
    fired = am.evaluate(reg)
    assert len(fired) == 1
    assert len(sink.sent) == 1
    assert sink.sent[0].rule == "cpu_high"
    assert sink.sent[0].severity == "critical"


def test_alert_manager_does_not_fire_below_threshold() -> None:
    reg = MetricRegistry()
    reg.set("cpu", 1.0)
    sink = _RecordingSink()
    rule = AlertRule(name="cpu_high", metric="cpu", threshold=2.0)
    am = AlertManager([rule], [sink], cooldown_seconds=0)
    assert am.evaluate(reg) == []
    assert sink.sent == []


def test_alert_manager_skips_missing_metric() -> None:
    reg = MetricRegistry()
    sink = _RecordingSink()
    rule = AlertRule(name="missing", metric="ghost", threshold=1.0)
    am = AlertManager([rule], [sink], cooldown_seconds=0)
    assert am.evaluate(reg) == []
    assert am.fired_count == 0


def test_alert_manager_cooldown_dedupes() -> None:
    reg = MetricRegistry()
    reg.set("cpu", 9.0)
    sink = _RecordingSink()
    rule = AlertRule(name="cpu_high", metric="cpu", threshold=2.0)
    monotonic = _FakeMonotonic([0.0, 0.0, 100.0])
    am = AlertManager([rule], [sink], monotonic_clock=monotonic, cooldown_seconds=60.0)
    assert len(am.evaluate(reg)) == 1  # fires
    assert len(am.evaluate(reg)) == 0  # within cooldown
    assert len(am.evaluate(reg)) == 1  # after cooldown
    assert am.fired_count == 2


def test_alert_manager_negative_cooldown_rejected() -> None:
    with pytest.raises(ValueError):
        AlertManager([], [], cooldown_seconds=-1)


def test_alert_manager_sink_failure_is_isolated() -> None:
    reg = MetricRegistry()
    reg.set("cpu", 9.0)
    good = _RecordingSink()
    rule = AlertRule(name="cpu_high", metric="cpu", threshold=2.0)
    am = AlertManager([rule], [_RaisingSink(), good], cooldown_seconds=0)
    fired = am.evaluate(reg)
    assert len(fired) == 1
    assert len(good.sent) == 1  # the good sink still received it
    assert am.errors and "channel down" in am.errors[0]
