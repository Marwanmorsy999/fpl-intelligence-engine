"""Health endpoint contract test.

Phase 18.0: made hermetic — the health route's ``get_db`` dependency is
overridden with an in-memory SQLite session so the test never depends on a
reachable PostgreSQL instance (CI/laptops have none; the real probe is covered
by ``test_health_db_probe.py``).
"""

from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.api import deps
from fpl_intelligence.api.main import app
from fpl_intelligence.db.base import Base


def test_health() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[deps._get_db_session] = _override_db
    try:
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    finally:
        app.dependency_overrides.pop(deps._get_db_session, None)
