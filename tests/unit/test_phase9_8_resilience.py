"""Phase 9.8 unit tests — Error Handling and Recovery.

Exercises the resilience layer (retry with exponential backoff, circuit breaker,
recovery manager with dead-lettering) with fully mocked clocks and sleep so no
wall-clock delay or network call ever happens inside ``pytest``.
"""
from __future__ import annotations

import pytest

from fpl_intelligence.deployment.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RecordingDeadLetterSink,
    RecoveryEntry,
    RecoveryManager,
    RecoveryReport,
    RetryOutcome,
    RetryPolicy,
    retry,
)


class _FakeClock:
    """A mutable monotonic clock whose value can be advanced deterministically."""

    def __init__(self, value: float = 0.0) -> None:
        self._value = value

    def __call__(self) -> float:
        return self._value

    def advance(self, delta: float) -> None:
        self._value += delta


class _RecordingSleep:
    """A fake ``SleepFn`` that records every delay it is asked to sleep for."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _recording_sleep() -> _RecordingSleep:
    return _RecordingSleep()


def test_retry_policy_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_seconds=-1.0)
    with pytest.raises(ValueError):
        RetryPolicy(multiplier=0.5)
    with pytest.raises(ValueError):
        RetryPolicy(retry_on=())


def test_retry_policy_delay_exponential_with_cap() -> None:
    policy = RetryPolicy(
        base_delay_seconds=1.0, max_delay_seconds=5.0, multiplier=2.0, jitter_seconds=0.0
    )
    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(2) == 2.0
    assert policy.delay_for(3) == 4.0
    assert policy.delay_for(10) == 5.0  # capped by max_delay


def test_retry_policy_jitter_adds_within_bounds() -> None:
    policy = RetryPolicy(base_delay_seconds=2.0, jitter_seconds=1.0, multiplier=1.0)
    delay = policy.delay_for(1)
    assert 2.0 <= delay <= 3.0


def test_retry_policy_is_retryable() -> None:
    policy = RetryPolicy(retry_on=(ValueError,))
    assert policy.is_retryable(ValueError("x"))
    assert not policy.is_retryable(KeyError("y"))


def test_retry_succeeds_first_attempt() -> None:
    policy = RetryPolicy(max_attempts=3, sleep=_recording_sleep())
    outcome = retry(lambda: "ok", policy)
    assert isinstance(outcome, RetryOutcome)
    assert outcome.success
    assert outcome.attempts == 1
    assert outcome.result == "ok"
    assert outcome.error is None


def test_retry_retries_then_succeeds() -> None:
    sleep = _recording_sleep()
    policy = RetryPolicy(max_attempts=3, sleep=sleep)
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("blip")
        return "recovered"

    outcome = retry(flaky, policy)
    assert outcome.success
    assert outcome.attempts == 3
    assert outcome.result == "recovered"
    assert calls["n"] == 3
    assert len(sleep.calls) == 2  # slept after attempts 1 and 2


def test_retry_exhausts_attempts() -> None:
    sleep = _recording_sleep()
    policy = RetryPolicy(max_attempts=3, sleep=sleep)
    outcome = retry(lambda: (_ for _ in ()).throw(RuntimeError("boom")), policy)
    assert not outcome.success
    assert outcome.attempts == 3
    assert isinstance(outcome.error, RuntimeError)
    assert len(sleep.calls) == 2  # no sleep after the final attempt


def test_retry_non_retryable_aborts_immediately() -> None:
    sleep = _recording_sleep()
    policy = RetryPolicy(max_attempts=5, retry_on=(ValueError,), sleep=sleep)
    outcome = retry(lambda: (_ for _ in ()).throw(KeyError("no")), policy)
    assert not outcome.success
    assert outcome.attempts == 1
    assert isinstance(outcome.error, KeyError)
    assert sleep.calls == []  # never slept — non-retryable


def test_retry_uses_injected_clock() -> None:
    clock = _FakeClock(0.0)
    policy = RetryPolicy(max_attempts=2, sleep=_recording_sleep(), clock=clock)
    outcome = retry(lambda: (_ for _ in ()).throw(RuntimeError()), policy)
    assert outcome.elapsed_seconds == 0.0  # clock frozen
    assert not outcome.success


def test_circuit_breaker_passes_when_closed() -> None:
    breaker = CircuitBreaker(failure_threshold=3, clock=_FakeClock())
    assert breaker.state is CircuitState.CLOSED
    assert breaker.call(lambda: 42) == 42
    assert breaker.consecutive_failures == 0


def test_circuit_breaker_opens_after_threshold() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout_seconds=10.0, clock=clock)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: 1)


def test_circuit_breaker_half_open_recovers_on_success() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=10.0, clock=clock)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert breaker.state is CircuitState.OPEN
    clock.advance(20.0)  # pass the reset window
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state is CircuitState.CLOSED
    assert breaker.consecutive_failures == 0


def test_circuit_breaker_half_open_reopens_on_failure() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=10.0, clock=clock)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    clock.advance(20.0)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("y")))
    assert breaker.state is CircuitState.OPEN


def test_circuit_breaker_reset() -> None:
    breaker = CircuitBreaker(failure_threshold=1, clock=_FakeClock())
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert breaker.state is CircuitState.OPEN
    breaker.reset()
    assert breaker.state is CircuitState.CLOSED


def test_circuit_breaker_stats_and_invalid_args() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(reset_timeout_seconds=-1.0)
    breaker = CircuitBreaker(failure_threshold=3, clock=_FakeClock())
    breaker.call(lambda: 1)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    stats = breaker.stats
    assert stats["calls"] == 2
    assert stats["successes"] == 1
    assert stats["failures"] == 1


def test_recovery_manager_execute_success() -> None:
    mgr = RecoveryManager()
    result = mgr.execute("op-1", lambda: "done")
    assert result == "done"
    assert mgr.report.succeeded == 1
    assert mgr.report.failed == 0


def test_recovery_manager_retries_then_recovers() -> None:
    mgr = RecoveryManager(retry_policy=RetryPolicy(max_attempts=3, sleep=_recording_sleep()))
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("blip")
        return "recovered"

    result = mgr.execute("op-flaky", flaky)
    assert result == "recovered"
    entry = mgr.report.entries[0]
    assert entry.success
    assert entry.recovered  # succeeded after more than one attempt
    assert mgr.report.recovered == 1


def test_recovery_manager_failure_dead_letters_and_reraises() -> None:
    sink = RecordingDeadLetterSink()
    mgr = RecoveryManager(
        retry_policy=RetryPolicy(max_attempts=2, sleep=_recording_sleep()),
        dead_letter_sink=sink,
    )
    with pytest.raises(RuntimeError):
        mgr.execute("op-bad", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert mgr.report.failed == 1
    assert mgr.report.succeeded == 0
    assert len(sink.entries) == 1
    assert not sink.entries[0].success
    assert "boom" in sink.entries[0].error


def test_recovery_manager_failure_no_raise_returns_none() -> None:
    sink = RecordingDeadLetterSink()
    mgr = RecoveryManager(
        retry_policy=RetryPolicy(max_attempts=2, sleep=_recording_sleep()),
        dead_letter_sink=sink,
    )
    result = mgr.execute(
        "op-bad",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        raise_on_failure=False,
    )
    assert result is None
    assert mgr.report.failed == 1
    assert len(sink.entries) == 1


def test_recovery_manager_circuit_open_path() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=10.0, clock=clock)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("seed")))
    assert breaker.state is CircuitState.OPEN
    sink = RecordingDeadLetterSink()
    mgr = RecoveryManager(
        retry_policy=RetryPolicy(max_attempts=3, sleep=_recording_sleep()),
        circuit_breaker=breaker,
        dead_letter_sink=sink,
    )
    with pytest.raises(CircuitOpenError):
        mgr.execute("op-blocked", lambda: "never")
    assert mgr.report.failed == 1
    assert "circuit open" in mgr.report.entries[0].error
    assert len(sink.entries) == 1


def test_recovery_report_and_entry_to_dict() -> None:
    entry = RecoveryEntry(
        operation_id="op-1",
        attempts=2,
        success=True,
        recovered=True,
    )
    report = RecoveryReport(entries=[entry])
    assert report.total_operations == 1
    assert report.succeeded == 1
    assert report.recovered == 1
    assert report.failed == 0
    data = report.to_dict()
    assert data["total_operations"] == 1
    assert data["entries"][0]["operation_id"] == "op-1"
    assert entry.to_dict()["recovered"] is True


def test_recording_dead_letter_sink_captures() -> None:
    sink = RecordingDeadLetterSink()
    entry = RecoveryEntry(operation_id="x", attempts=1, success=False, error="e")
    sink.write(entry)
    sink.write(entry)
    assert len(sink.entries) == 2


def test_recovery_manager_recovers_while_other_stage_continues() -> None:
    """A failed operation must not abort the recovery manager for later ops."""
    mgr = RecoveryManager(
        retry_policy=RetryPolicy(max_attempts=1, sleep=_recording_sleep())
    )
    with pytest.raises(RuntimeError):
        mgr.execute("bad", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    good = mgr.execute("good", lambda: 7)
    assert good == 7
    assert mgr.report.total_operations == 2
    assert mgr.report.succeeded == 1
    assert mgr.report.failed == 1
