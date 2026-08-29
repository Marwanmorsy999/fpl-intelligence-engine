"""Low-overhead production request profiling for Phase 0.

The middleware deliberately uses only process-local timing and the existing
application logger. It does not add a database write, external telemetry call,
or response-body buffering. Each request receives a correlation id and emits a
single structured timing record suitable for Vercel runtime-log aggregation.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("fpl.performance")
_request_id: ContextVar[str] = ContextVar("fpl_request_id", default="-")


@dataclass(slots=True)
class PhaseTimer:
    """Collect named phase durations in milliseconds within one request."""

    started: float = field(default_factory=time.perf_counter)
    phases: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.phases[name] = (time.perf_counter() - start) * 1000.0

    @property
    def total_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000.0


def current_request_id() -> str:
    """Return the correlation id for the current request context."""
    return _request_id.get()


class RequestProfilingMiddleware(BaseHTTPMiddleware):
    """Emit one request timing record and attach an ``X-Request-ID`` header."""

    async def dispatch(self, request: Request, call_next) -> Response:
        supplied = request.headers.get("x-request-id", "").strip()
        request_id = supplied[:128] if supplied else uuid.uuid4().hex
        token = _request_id.set(request_id)
        timer = PhaseTimer()
        request.state.performance = timer

        try:
            response = await call_next(request)
        except Exception:
            elapsed = timer.total_ms
            logger.exception(
                "phase0_request method=%s path=%s status=500 total_ms=%.2f request_id=%s",
                request.method,
                request.url.path,
                elapsed,
                request_id,
            )
            raise
        finally:
            _request_id.reset(token)

        response.headers.setdefault("X-Request-ID", request_id)
        logger.info(
            "phase0_request method=%s path=%s status=%s total_ms=%.2f phases=%s request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            timer.total_ms,
            ",".join(f"{k}:{v:.2f}" for k, v in timer.phases.items()),
            request_id,
        )
        return response


def phase_timer(request: Request) -> PhaseTimer:
    """Return the request-local profiler, creating one for direct/test calls."""
    timer: Any = getattr(request.state, "performance", None)
    if isinstance(timer, PhaseTimer):
        return timer
    timer = PhaseTimer()
    request.state.performance = timer
    return timer
