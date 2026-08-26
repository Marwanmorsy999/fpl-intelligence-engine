"""v2.7.7-league-regression — middleware scope + refresh never-500 contract.

Root-cause (v2.7.6): league_refresh() left _ensure_tables(db), db.get(), and
other DB calls OUTSIDE a wrapping try/except, while league_overview() wrapped
its entire impl in one.  On Vercel cold-starts the route body could throw
before the per-request exception handlers had a chance to catch it; in
integration tests the ServerErrorMiddleware only rescues if the exception
escapes the route handler, and add_exception_handler(Exception, …) does the
same — but both are moot when the route never returns.

This file proves:
  1. api/index.py exports the SAME app object that carries ServerErrorMiddleware.
  2. Forcing a DB exception inside league_refresh (and league_overview and
     league_trajectory) returns HTTP 200 with a degraded/error chip — never 500.
  3. The regression introduced in v2.7.6 is gone: league_refresh now wraps its
     entire body in a never-500 try/except, matching league_overview's contract.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_override(db_session: Session) -> Generator[Session, None, None]:
    yield db_session


def _exploding_db_override() -> Generator[Session, None, None]:
    """Yield a session whose every operation raises OperationalError."""
    bad = MagicMock(spec=Session)
    _exc = OperationalError("connection timeout", {}, Exception("timeout"))

    def _raise(*_a: object, **_kw: object) -> None:
        raise _exc

    bad.execute.side_effect = _raise
    bad.get.side_effect = _raise
    bad.scalar.side_effect = _raise
    bad.get_bind.side_effect = _raise
    bad.rollback.return_value = None
    bad.commit.return_value = None
    yield bad


# ---------------------------------------------------------------------------
# Step 3: Middleware scope check — api/index.py must export the same app
# ---------------------------------------------------------------------------


class TestMiddlewareScope:
    """api/index.py must export the exact app object with exception handlers."""

    def test_index_app_is_main_app(self) -> None:
        """The Vercel entrypoint re-exports the same FastAPI app as main.py."""
        from api.index import app as index_app  # type: ignore[import]
        from fpl_intelligence.api.main import app as main_app

        assert index_app is main_app, (
            "api/index.py exports a DIFFERENT app object than main.py — "
            "the ServerErrorMiddleware never-500 chain is broken for Vercel."
        )

    def test_index_app_has_exception_handler(self) -> None:
        """The exported app carries the Exception → degraded-JSON handler."""
        from api.index import app as index_app  # type: ignore[import]

        # FastAPI stores exception handlers on the ExceptionMiddleware layer;
        # the handler dict is accessible via app.exception_handlers.
        handlers = dict(index_app.exception_handlers)
        assert Exception in handlers, (
            "add_exception_handler(Exception, …) is not registered on the "
            "app object exported by api/index.py."
        )

    def test_db_exception_through_index_app_returns_200_degraded(
        self, db_session: Session
    ) -> None:
        """Forcing a DB failure via the index app never returns a raw 500."""
        from api.index import app as index_app  # type: ignore[import]

        index_app.dependency_overrides[deps._get_db_session] = _exploding_db_override
        try:
            client = TestClient(index_app, raise_server_exceptions=False)
            r = client.get("/api/v1/league", params={"session_id": "2295006"})
            assert r.status_code == 200, (
                f"Expected 200 degraded, got {r.status_code}: {r.text[:400]}"
            )
            body = r.json()
            assert body.get("status") in {"degraded", "error", "no-league", "stale"}, body
        finally:
            index_app.dependency_overrides.pop(deps._get_db_session, None)


# ---------------------------------------------------------------------------
# Step 4 (regression proof): league_refresh body is now inside never-500
# ---------------------------------------------------------------------------


class TestLeagueRefreshNever500:
    """POST /league/refresh must return 200 even when _ensure_tables blows up."""

    def test_refresh_db_exception_returns_200_error_chip(
        self, db_session: Session
    ) -> None:
        from fpl_intelligence.api.main import app

        app.dependency_overrides[deps._get_db_session] = _exploding_db_override
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post(
                "/api/v1/league/refresh",
                json={"session_id": "2295006"},
            )
            assert r.status_code == 200, (
                f"league_refresh should never 500, got {r.status_code}: {r.text[:400]}"
            )
            body = r.json()
            # Must carry an honest status — never raw traceback
            assert body.get("status") in {
                "error", "no-league", "refreshing", "stale", "ok"
            }, body
            assert "Traceback" not in r.text
        finally:
            app.dependency_overrides.pop(deps._get_db_session, None)

    def test_refresh_none_session_never_500(self, db_session: Session) -> None:
        """'None' session_id must not crash league_refresh — v2.7.6 guard still works."""
        from fpl_intelligence.api.main import app

        app.dependency_overrides[deps._get_db_session] = lambda: _db_override(db_session)
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post(
                "/api/v1/league/refresh",
                json={"session_id": "None"},
            )
            assert r.status_code == 200, r.text[:400]
            body = r.json()
            assert body.get("status") in {"no-league", "error"}, body
            assert "Traceback" not in r.text
        finally:
            app.dependency_overrides.pop(deps._get_db_session, None)


# ---------------------------------------------------------------------------
# Existing never-500 contracts still hold post-v2.7.7
# ---------------------------------------------------------------------------


class TestLeagueOverviewNever500:
    """GET /league must return 200 even when the DB explodes (v2.7.4 contract)."""

    def test_league_overview_db_exception_returns_200(
        self, db_session: Session
    ) -> None:
        from fpl_intelligence.api.main import app

        app.dependency_overrides[deps._get_db_session] = _exploding_db_override
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.get("/api/v1/league", params={"session_id": "2295006"})
            assert r.status_code == 200, (
                f"league_overview must never 500, got {r.status_code}: {r.text[:400]}"
            )
            body = r.json()
            assert body.get("status") in {"degraded", "error", "no-league", "stale"}, body
        finally:
            app.dependency_overrides.pop(deps._get_db_session, None)


class TestLeagueTrajectoryNever500:
    """GET /league/trajectory must return 200 even when DB explodes."""

    def test_trajectory_db_exception_returns_200(self, db_session: Session) -> None:
        from fpl_intelligence.api.main import app

        app.dependency_overrides[deps._get_db_session] = _exploding_db_override
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.get(
                "/api/v1/league/trajectory", params={"session_id": "2295006"}
            )
            assert r.status_code == 200, (
                f"league/trajectory must never 500, got {r.status_code}: {r.text[:400]}"
            )
            body = r.json()
            assert body.get("status") in {
                "unavailable", "no-league", "no-cache", "no-predictions", "degraded"
            }, body
        finally:
            app.dependency_overrides.pop(deps._get_db_session, None)
