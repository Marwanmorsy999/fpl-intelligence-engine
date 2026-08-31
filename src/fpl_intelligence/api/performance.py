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
from functools import wraps
from typing import Any, Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse
from starlette.responses import PlainTextResponse
from starlette.responses import Response

logger = logging.getLogger("fpl.performance")
_request_id: ContextVar[str] = ContextVar("fpl_request_id", default="-")
_current_timer: ContextVar["PhaseTimer | None"] = ContextVar(
    "fpl_phase_timer", default=None
)


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
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self.phases[name] = self.phases.get(name, 0.0) + elapsed_ms

    @property
    def total_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000.0


def current_request_id() -> str:
    """Return the correlation id for the current request context."""
    return _request_id.get()


def current_phase_timer() -> PhaseTimer | None:
    """Return the active request profiler, if this code runs inside a request."""
    return _current_timer.get()


_SERIALIZATION_WRAPPED = False
_FPL_EGRESS_WRAPPED = False


def _wrap_response_render(response_cls: type[Response]) -> None:
    """Time the actual response-body rendering for one Starlette response class."""
    marker = "_fpl_phase0_render_wrapped"
    if getattr(response_cls, marker, False):
        return
    original = response_cls.render

    @wraps(original)
    def render_with_timing(self: Response, content: Any) -> bytes:
        timer = current_phase_timer()
        if timer is None:
            return original(self, content)
        with timer.phase("serialization"):
            return original(self, content)

    response_cls.render = render_with_timing  # type: ignore[method-assign]
    setattr(response_cls, marker, True)


def install_serialization_timing() -> None:
    """Install process-wide response rendering hooks once.

    The hooks are limited to Starlette's built-in response classes used by this
    API. Only requests with an active PhaseTimer incur timing work.
    """
    global _SERIALIZATION_WRAPPED
    if _SERIALIZATION_WRAPPED:
        return
    for response_cls in (Response, JSONResponse, PlainTextResponse, HTMLResponse):
        _wrap_response_render(response_cls)
    _SERIALIZATION_WRAPPED = True


def install_fpl_egress_timing() -> None:
    """Instrument FPL egress calls without changing their result semantics."""
    global _FPL_EGRESS_WRAPPED
    if _FPL_EGRESS_WRAPPED:
        return

    try:
        from fpl_intelligence.data_providers.fpl_egress import FplEgressChain
    except Exception:  # pragma: no cover - import availability is environment-specific
        logger.debug("phase0: FPL egress hook unavailable", exc_info=True)
        return

    marker = "_fpl_phase0_egress_wrapped"
    if getattr(FplEgressChain, marker, False):
        _FPL_EGRESS_WRAPPED = True
        return

    original_fetch = FplEgressChain.fetch
    original_fetch_text = FplEgressChain.fetch_text

    async def fetch_with_timing(self: Any, *args: Any, **kwargs: Any) -> Any:
        timer = current_phase_timer()
        if timer is None:
            return await original_fetch(self, *args, **kwargs)
        with timer.phase("fpl_egress"):
            return await original_fetch(self, *args, **kwargs)

    async def fetch_text_with_timing(self: Any, *args: Any, **kwargs: Any) -> Any:
        timer = current_phase_timer()
        if timer is None:
            return await original_fetch_text(self, *args, **kwargs)
        with timer.phase("fpl_egress"):
            return await original_fetch_text(self, *args, **kwargs)

    FplEgressChain.fetch = fetch_with_timing  # type: ignore[method-assign]
    FplEgressChain.fetch_text = fetch_text_with_timing  # type: ignore[method-assign]
    setattr(FplEgressChain, marker, True)
    _FPL_EGRESS_WRAPPED = True


@event.listens_for(Engine, "before_cursor_execute")
def _phase0_db_start(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """Start request-local DB timing for SQLAlchemy statements."""
    if current_phase_timer() is None:
        return
    context._fpl_phase0_db_started = time.perf_counter()


@event.listens_for(Engine, "after_cursor_execute")
def _phase0_db_end(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """Accumulate request-local DB execution time for SQLAlchemy statements."""
    started = getattr(context, "_fpl_phase0_db_started", None)
    timer = current_phase_timer()
    if started is None or timer is None:
        return
    timer.phases["db"] = timer.phases.get("db", 0.0) + (
        time.perf_counter() - started
    ) * 1000.0


class RequestProfilingMiddleware(BaseHTTPMiddleware):
    """Emit one request timing record and attach an ``X-Request-ID`` header."""

    async def dispatch(self, request: Request, call_next) -> Response:
        supplied = request.headers.get("x-request-id", "").strip()
        request_id = supplied[:128] if supplied else uuid.uuid4().hex
        id_token = _request_id.set(request_id)
        timer = PhaseTimer()
        timer_token = _current_timer.set(timer)
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
            _current_timer.reset(timer_token)
            _request_id.reset(id_token)

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
