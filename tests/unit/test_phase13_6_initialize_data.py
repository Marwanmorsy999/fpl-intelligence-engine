"""Phase 13.6 — one-time initialize-data endpoint tests.

POST /api/v1/admin/initialize-data is a temporary, UNAUTHENTICATED bootstrap
that seeds teams + prices from the committed FPL seed and then disables itself.
These tests prove it:

* seeds PlayerTeamMembership + PlayerGameweekPerformance (price = now_cost/10);
* makes GET /api/v1/players return non-null team + price;
* self-disables — a second call returns 410 (zero rows re-seeded);
* is not gated on CRON_SECRET (it is deliberately a one-shot bootstrap).

Fully offline: the admin ``SessionLocal`` is redirected to an in-memory SQLite
database and ``_resolve_seed_path`` is pointed at a temp seed file.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.api import deps
from fpl_intelligence.api.routes import admin
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import IngestionRun

_SAMPLE_SEED = {
    "meta": {"season_code": "2026-27"},
    "teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"}],
    "events": [{"id": 1, "name": "Gameweek 1"}],
    "players": [
        {
            "id": 411,
            "first_name": "Mohamed",
            "second_name": "Salah",
            "web_name": "M. Salah",
            "position": 3,
            "team": 1,
            "now_cost": 130,
        },
        {
            "id": 412,
            "first_name": "Erling",
            "second_name": "Haaland",
            "web_name": "Haaland",
            "position": 4,
            "team": 1,
            "now_cost": 140,
        },
    ],
}


@pytest.fixture
def sessionmaker_factory() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture
def client(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    sessionmaker_factory: sessionmaker,
) -> TestClient:
    monkeypatch.setattr(admin, "SessionLocal", sessionmaker_factory)
    seed_path = tmp_path / "fpl_bootstrap_seed.json"
    seed_path.write_text(json.dumps(_SAMPLE_SEED), encoding="utf-8")
    monkeypatch.setattr(admin, "_resolve_seed_path", lambda: seed_path)
    # Deliberately NOT setting / removing CRON_SECRET auth: this endpoint is
    # unauthenticated by design. Remove any ambient secret to keep it open.
    monkeypatch.delenv("CRON_SECRET", raising=False)

    from fpl_intelligence.api.main import app

    def _override_db() -> Session:
        yield sessionmaker_factory()

    app.dependency_overrides[deps._get_db_session] = _override_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_initialize_seeds_teams_and_prices(client: TestClient) -> None:
    resp = client.post("/api/v1/admin/initialize-data")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["initialized"] is True
    assert body["players"] == 2
    assert body["memberships_created"] == 2
    assert body["performances_created"] == 2

    # Players endpoint now returns non-null team + price for every seeded row.
    rows = client.get("/api/v1/players").json()
    by_name = {p["web_name"]: p for p in rows}
    assert by_name["M. Salah"]["team"] is not None
    assert by_name["M. Salah"]["price"] == pytest.approx(13.0)
    assert by_name["Haaland"]["team"] is not None
    assert by_name["Haaland"]["price"] == pytest.approx(14.0)


def test_initialize_self_disables_after_first_run(client: TestClient) -> None:
    first = client.post("/api/v1/admin/initialize-data")
    assert first.status_code == 200

    second = client.post("/api/v1/admin/initialize-data")
    assert second.status_code == 410, second.text
    body = second.json()
    assert body["ok"] is False
    assert "already initialized" in body["error"].lower()


def test_initialize_records_successful_ingestion_run(client: TestClient) -> None:
    client.post("/api/v1/admin/initialize-data")
    db = admin.SessionLocal()
    try:
        run = db.query(IngestionRun).filter_by(job_name="initialize-data").first()
        assert run is not None
        assert run.status == "SUCCESS"
        assert run.records_processed == 2
    finally:
        db.close()


def test_initialize_requires_no_cron_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with a configured CRON_SECRET the one-shot bootstrap stays open.
    monkeypatch.setenv("CRON_SECRET", "secret")
    resp = client.post("/api/v1/admin/initialize-data")
    assert resp.status_code == 200
