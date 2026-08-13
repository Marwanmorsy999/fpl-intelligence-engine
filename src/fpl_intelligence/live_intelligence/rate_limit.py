"""Phase 9.1 pacing and budget controls for free-tier LLM access.

Two independent failure modes, two independent guards.

**Rate** — free tiers publish a requests-per-minute ceiling and answer with
HTTP 429 when it is crossed. :class:`RateLimiter` spaces calls by a minimum
interval so the ceiling is respected *before* the request is made. Preventing
the 429 is strictly better than retrying it: a retry still consumed a request
slot, and on some tiers a burst of 429s triggers a longer cool-off.

**Volume** — a bug, a large batch, or an unattended loop can burn a daily quota
in seconds. :class:`CallBudget` caps how many live calls a single process may
make, so the worst case is a clear exception rather than an exhausted account.
Cache hits deliberately do **not** consume budget; they are not API calls.

Both take injected ``clock`` and ``sleep`` callables. Tests therefore assert on
the exact pacing decisions with no wall-clock delay, which is the only way this
behaviour can be covered at all in a suite that must stay fast and offline.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: Monotonic time source, in seconds. Injected so tests control the timeline.
MonotonicClock = Callable[[], float]
SleepFn = Callable[[float], None]


class CallBudgetExceededError(RuntimeError):
    """Raised when a process asks for more live calls than it was allotted."""


@dataclass
class RateLimiterStats:
    """What the limiter actually did, for the dry-run report."""

    calls: int = 0
    sleeps: int = 0
    total_sleep_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "sleeps": self.sleeps,
            "total_sleep_seconds": round(self.total_sleep_seconds, 3),
        }


class RateLimiter:
    """Enforce a minimum interval between successive live calls.

    Deliberately the simplest thing that works: no token bucket, no jitter, no
    concurrency. Phase 9.1 extraction is sequential and low-volume, and a
    simple guard that is obviously correct is worth more here than a
    sophisticated one whose failure mode is a suspended free-tier account.

    Args:
        min_interval_seconds: Minimum gap between calls. ``0`` disables pacing.
        clock: Monotonic time source.
        sleep: Sleep function, called only with a strictly positive delay.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: MonotonicClock = time.monotonic,
        sleep: SleepFn = time.sleep,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must not be negative")
        self._min_interval = float(min_interval_seconds)
        self._clock = clock
        self._sleep = sleep
        self._last_call_at: float | None = None
        self.stats = RateLimiterStats()

    @property
    def min_interval_seconds(self) -> float:
        return self._min_interval

    def acquire(self) -> float:
        """Block until the next call is allowed. Returns seconds actually slept.

        The first call never waits: pacing constrains the *gap* between calls,
        and delaying a cold start would be pure latency with no quota benefit.
        """
        now = self._clock()
        slept = 0.0
        if self._last_call_at is not None and self._min_interval > 0:
            elapsed = now - self._last_call_at
            remaining = self._min_interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
                slept = remaining
                now = self._clock()
                self.stats.sleeps += 1
                self.stats.total_sleep_seconds += remaining
        self._last_call_at = now
        self.stats.calls += 1
        return slept

    def pause(self, seconds: float) -> float:
        """Sleep for an externally mandated cool-off, e.g. a ``Retry-After``.

        Recorded in the same statistics as ordinary pacing so the dry-run
        report shows the true total time spent waiting on the provider.
        """
        if seconds <= 0:
            return 0.0
        self._sleep(seconds)
        self.stats.sleeps += 1
        self.stats.total_sleep_seconds += seconds
        self._last_call_at = self._clock()
        return seconds

    def reset(self) -> None:
        """Forget the last call time. Used between independent runs."""
        self._last_call_at = None


class CallBudget:
    """Hard ceiling on the number of live API calls in one process.

    Args:
        max_calls: Maximum live calls permitted. Must be at least 1.
        label: Included in the error message so a caller knows which budget
            was exhausted when more than one is in play.
    """

    def __init__(self, max_calls: int, *, label: str = "llm") -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        self._max_calls = int(max_calls)
        self._used = 0
        self._label = label

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self._max_calls - self._used)

    @property
    def exhausted(self) -> bool:
        return self.remaining == 0

    def consume(self, count: int = 1) -> int:
        """Claim ``count`` calls, or refuse the whole request.

        Refusal is all-or-nothing: a partially granted budget would let a
        caller proceed with a request it cannot complete, which is worse than
        stopping.
        """
        if count < 1:
            raise ValueError("count must be at least 1")
        if self._used + count > self._max_calls:
            raise CallBudgetExceededError(
                f"{self._label} call budget exhausted: {self._used}/{self._max_calls} "
                f"live calls already made, {count} more requested. This ceiling exists "
                "to protect a free-tier quota. Raise LLM_MAX_CALLS_PER_RUN only if you "
                "know the provider's remaining allowance."
            )
        self._used += count
        return self.remaining

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_calls": self._max_calls,
            "used": self._used,
            "remaining": self.remaining,
        }
