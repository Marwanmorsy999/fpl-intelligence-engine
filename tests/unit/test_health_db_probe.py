"""Health endpoint DB probe tests.

Two layers:

* Unit — ``_probe_database`` against a mocked session (success + failure).
* Endpoint — ``GET /api/v1/health`` with ``deps._get_db_session`` overridden to
  force the DB DOWN path and to use the in-memory ``db_session`` fixture for the
  UP path. Asserts on the explicit ``database`` contract fields directly to avoid
  cross-test contamination of the module-level ``_MONITORING`` singleton.
"""
from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from fpl_intelligence import live_intelligence  # noqa: F401  (register models)
from fpl_intelligence.api import deps
from fpl_intelligence.api.routes.intelligence import _probe_database


def test_probe_database_success() -> None:
    fake = MagicMock(spec=Session)
    fake.execute.return_value.scalar_one.return_value = 1
    ok, detail = _probe_database(fake)
    assert ok is True
    assert detail == "postgres/sqlite reachable"
    fake.execute.assert_called_once()


def test_probe_database_failure() -> None:
    fake = MagicMock(spec=Session)
    fake.execute.side_effect = SQLAlchemyError("connection refused")
    ok, detail = _probe_database(fake)
    assert ok is False
    assert "connection refused" in detail


@pytest.fixture
def probe_client(db_session: Session) -> Generator[TestClient, None, None]:
    from fpl_intelligence.api.main import app

    app.dependency_overrides[deps._get_db_session] = lambda: db_session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_health_endpoint_database_up(probe_client: TestClient) -> None:
    resp = probe_client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["database"]["ok"] is True
    assert body["database"]["status"] == "up"
    assert body["api"]["ok"] is True
    assert body["api"]["status"] == "up"
    assert body["status"] == "ok"


def test_health_endpoint_database_down() -> None:
    from fpl_intelligence.api.main import app

    def _down_db() -> Generator[Session, None, None]:
        fake = MagicMock(spec=Session)
        fake.execute.side_effect = SQLAlchemyError("pool exhausted")
        yield fake

    app.dependency_overrides[deps._get_db_session] = _down_db
    try:
        with TestClient(app) as client:
            resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["database"]["ok"] is False
        assert body["database"]["status"] == "down"
        assert "pool exhausted" in body["database"]["detail"]
        assert body["status"] == "degraded"
    finally:
        app.dependency_overrides.clear()
