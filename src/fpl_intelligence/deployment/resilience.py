"""Phase 9.8 — Error Handling and Recovery.

The deployed system must survive transient failures (flaky sources, webhook
timeouts, database blips) without human intervention.

* :class:`RetryPolicy` — exponential backoff with optional jitter, injectable
  ``sleep``/``clock`` seams and a configurable ``retry_on`` exception family.
  :func:`retry` wraps an operation and returns a :class:`RetryOutcome` (never
  raises for retryable failures until attempts are exhausted).
* :class:`CircuitBreaker` — after ``failure_threshold`` consecutive failures the
  breaker opens and rejects calls with :class:`CircuitOpenError` until the
  ``reset_timeout_seconds`` reset window passes; then it allows a single
  half-open trial that closes or reopens the circuit.
* :class:`RecoveryManager` — the coordination layer: gate an operation behind
  the breaker, retry it on failure, record a :class:`RecoveryEntry`, optionally
  write permanently-failed operations to a :class:`DeadLetterSink`, and report
  everything on a :class:`RecoveryReport`.

All timers are injectable, so tests control the timeline exactly and **no**
wall-clock sleeps or network calls happen inside ``pytest``. This module is
additive: it does not modify the quantitative Phases 1–8 stack, and it hardcodes
no credentials or thresholds beyond safe defaults (overridable via the
production configuration).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from fpl_intelligence.live_intelligence.rate_limit import MonotonicClock, SleepFn
from fpl_intelligence.live_intelligence.temporal_ledger import Clock, utc_now


@dataclass(frozen=True)
class RetryPolicy:
    """How many times and how long to wait before giving up on an operation."""

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    multiplier: float = 2.0
    jitter_seconds: float = 0.0
    retry_on: tuple[type[BaseException], ...] = (Exception,)
    sleep: SleepFn = time.sleep
    clock: MonotonicClock = time.monotonic

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must not be negative")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds must not be negative")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be at least 1.0")
        if self.jitter_seconds < 0:
            raise ValueError("jitter_seconds must not be negative")
        if not self.retry_on:
            raise ValueError("retry_on must not be empty")

    def delay_for(self, failed_attempts: int) -> float:
        """Exponential-backoff delay before the next attempt.

        ``failed_attempts`` is the number of failures observed so far
        (``1`` -> ``base_delay``, ``2`` -> ``base * multiplier``, ...), capped by
        ``max_delay_seconds``, with optional uniform jitter added.
        """
        exponent = max(0, int(failed_attempts) - 1)
        delay = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (self.multiplier**exponent),
        )
        if self.jitter_seconds > 0:
            delay += random.uniform(0.0, self.jitter_seconds)
        return max(0.0, delay)

    def is_retryable(self, exc: BaseException) -> bool:
        return isinstance(exc, self.retry_on)


@dataclass
class RetryOutcome:
    """What :func:`retry` observed: all attempts, the result or the final error."""

    attempts: int
    success: bool
    result: Any = None
    error: BaseException | None = None
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "success": self.success,
            "error": f"{type(self.error).__name__}: {self.error}" if self.error else None,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


def retry(op: Callable[[], Any], policy: RetryPolicy) -> RetryOutcome:
    """Run ``op`` under ``policy``; do not raise for retryable exhaustion.

    A non-retryable exception aborts immediately. Tree-safe: tests inject a fake
    ``sleep``/``clock`` so no wall-clock delay ever happens inside ``pytest``.
    """
    start = policy.clock()
    failures = 0
    last_error: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            result = op()
            return RetryOutcome(
                attempts=attempt,
                success=True,
                result=result,
                elapsed_seconds=policy.clock() - start,
            )
        except Exception as exc:  # noqa: BLE001 - the policy owns exception handling
            failures = attempt
            last_error = exc
            if not policy.is_retryable(exc) or attempt >= policy.max_attempts:
                break
            policy.sleep(policy.delay_for(failures))
    return RetryOutcome(
        attempts=failures,
        success=False,
        error=last_error,
        elapsed_seconds=policy.clock() - start,
    )


class CircuitState(StrEnum):
    """Lifecycle of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when the circuit is open; the caller should back off, not retry."""


class CircuitBreaker:
    """Stop hammering a failing dependency.

    Closed: calls pass through and count successes/failures. After
    ``failure_threshold`` consecutive failures the breaker opens: calls raise
    :class:`CircuitOpenError` immediately. After ``reset_timeout_seconds`` it
    lets a single trial through (half-open); a success closes it, a failure
    reopens it. The clock is injectable so tests freeze/advance time precisely.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 60.0,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if reset_timeout_seconds < 0:
            raise ValueError("reset_timeout_seconds must not be negative")
        self._failure_threshold = int(failure_threshold)
        self._reset_timeout_seconds = float(reset_timeout_seconds)
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._total_calls = 0
        self._total_successes = 0
        self._total_failures = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def failure_threshold(self) -> int:
        return self._failure_threshold

    @property
    def reset_timeout_seconds(self) -> float:
        return self._reset_timeout_seconds

    @property
    def stats(self) -> dict[str, int]:
        return {
            "calls": self._total_calls,
            "successes": self._total_successes,
            "failures": self._total_failures,
        }

    def call(self, op: Callable[[], Any]) -> Any:
        """Execute ``op`` via the breaker; raises CircuitOpenError while open."""
        if self._state is CircuitState.OPEN:
            if (
                self._opened_at is None
                or self._clock() - self._opened_at < self._reset_timeout_seconds
            ):
                raise CircuitOpenError(
                    f"circuit is open after {self._consecutive_failures} "
                    f"consecutive failures (reset in "
                    f"{self._reset_timeout_seconds}s)"
                )
            self._state = CircuitState.HALF_OPEN  # allow one trial
        self._total_calls += 1
        try:
            result = op()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def record_success(self) -> None:
        """Close the circuit after a success (used by integrations + tests)."""
        self._total_successes += 1
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        """Count a failure; open (or reopen) the circuit when warranted."""
        self._total_failures += 1
        self._consecutive_failures += 1
        if (
            self._state is CircuitState.HALF_OPEN
            or self._consecutive_failures >= self._failure_threshold
        ):
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()

    def reset(self) -> None:
        """Manually return the breaker to a closed state."""
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "failure_threshold": self._failure_threshold,
            "reset_timeout_seconds": self._reset_timeout_seconds,
            "consecutive_failures": self._consecutive_failures,
            "stats": self.stats,
        }


@dataclass
class RecoveryEntry:
    """What happened to one operation in the recovery pipeline."""

    operation_id: str
    attempts: int
    success: bool
    error: str | None = None
    recovered: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "attempts": self.attempts,
            "success": self.success,
            "recovered": self.recovered,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
        }


@dataclass
class RecoveryReport:
    """Aggregate view over every operation the recovery manager has run."""

    entries: list[RecoveryEntry] = field(default_factory=list)

    @property
    def total_operations(self) -> int:
        return len(self.entries)

    @property
    def succeeded(self) -> int:
        return sum(1 for entry in self.entries if entry.success)

    @property
    def recovered(self) -> int:
        return sum(1 for entry in self.entries if entry.recovered)

    @property
    def failed(self) -> int:
        return self.total_operations - self.succeeded

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_operations": self.total_operations,
            "succeeded": self.succeeded,
            "recovered": self.recovered,
            "failed": self.failed,
            "entries": [entry.to_dict() for entry in self.entries],
        }


class DeadLetterSink(Protocol):
    """Persistent store for operations that exhausted every recovery attempt."""

    def write(self, entry: RecoveryEntry) -> None: ...


class RecordingDeadLetterSink:
    """In-memory dead-letter sink (tests, dry-runs, small deployments)."""

    def __init__(self) -> None:
        self.entries: list[RecoveryEntry] = []

    def write(self, entry: RecoveryEntry) -> None:
        self.entries.append(entry)


class RecoveryManager:
    """Gate an operation behind the breaker, retry it, and record the outcome.

    ``execute`` never swallows a final failure by default: it re-raises the last
    error after dead-lettering it, so callers can decide. ``raise_on_failure=False``
    makes it return ``None`` instead (for fire-and-forget pipeline stages).
    """

    def __init__(
        self,
        *,
        retry_policy: RetryPolicy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        dead_letter_sink: DeadLetterSink | None = None,
        clock: Clock = utc_now,
        logger: logging.Logger | None = None,
    ) -> None:
        self._policy = retry_policy or RetryPolicy()
        self._breaker = circuit_breaker or CircuitBreaker()
        self._dead_letter = dead_letter_sink
        self._clock = clock
        self._logger = logger or logging.getLogger("fpl_intelligence.deployment.resilience")
        self._report = RecoveryReport()

    @property
    def report(self) -> RecoveryReport:
        return self._report

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def retry_policy(self) -> RetryPolicy:
        return self._policy

    def execute(
        self,
        operation_id: str,
        op: Callable[[], Any],
        *,
        raise_on_failure: bool = True,
    ) -> Any:
        started = self._clock()
        try:
            result = self._breaker.call(op)
        except CircuitOpenError as exc:
            entry = self._append(
                operation_id,
                attempts=0,
                success=False,
                error=f"circuit open: {exc}",
                started=started,
            )
            self._dead_letter_write(entry)
            if raise_on_failure:
                raise
            return None
        except Exception:  # noqa: BLE001 - the recovery pipeline owns this
            outcome = retry(op, self._policy)
            entry = self._append(
                operation_id,
                attempts=outcome.attempts,
                success=outcome.success,
                error=(
                    f"{type(outcome.error).__name__}: {outcome.error}"
                    if outcome.error is not None
                    else None
                ),
                recovered=outcome.success and outcome.attempts > 1,
                started=started,
            )
            if not outcome.success:
                self._logger.error(
                    "operation %s failed after %d attempt(s): %s",
                    operation_id,
                    outcome.attempts,
                    outcome.error,
                )
                self._dead_letter_write(entry)
                if raise_on_failure and outcome.error is not None:
                    raise outcome.error from None
                return None
            return outcome.result
        self._append(
            operation_id,
            attempts=1,
            success=True,
            error=None,
            recovered=False,
            started=started,
        )
        return result

    def _append(
        self,
        operation_id: str,
        *,
        attempts: int,
        success: bool,
        error: str | None,
        started: datetime,
        recovered: bool = False,
    ) -> RecoveryEntry:
        entry = RecoveryEntry(
            operation_id=operation_id,
            attempts=attempts,
            success=success,
            error=error,
            recovered=recovered,
            started_at=started,
            ended_at=self._clock(),
        )
        self._report.entries.append(entry)
        return entry

    def _dead_letter_write(self, entry: RecoveryEntry) -> None:
        if self._dead_letter is not None:
            self._dead_letter.write(entry)
