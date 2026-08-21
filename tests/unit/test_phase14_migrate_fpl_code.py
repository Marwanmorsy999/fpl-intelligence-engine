"""Phase 14.0 — one-shot migrate-fpl-code endpoint tests.

POST /api/v1/admin/migrate-fpl-code applies the pending ``players.fpl_code``
schema change to a database created before migration 0015 and backfills every
player's FPL element code from the bootstrap seed. These tests prove it:

* adds the missing column when the schema predates migration 0015;
* backfills fpl_code so GET /api/v1/players exposes photo-ready codes;
* is a no-op ALTER when the column already exists (fresh schema);
* self-disables — a second call returns 410.

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
            "code": 308,
        },
        {
            "id": 412,
            "first_name": "Erling",
            "second_name": "Haaland",
            "web_name": "Haaland",
            "position": 4,
            "team": 1,
            "now_cost": 140,
            "code": 309,
        },
    ],
}


def _make_sessionmaker(drop_code_column: bool) -> sessionmaker:
    """Create an app-schema SQLite DB, optionally simulating pre-0015 schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    if drop_code_column:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE players DROP COLUMN fpl_code")
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture
def client(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    sl = _make_sessionmaker(drop_code_column=True)
    monkeypatch.setattr(admin, "SessionLocal", sl)
    seed_path = tmp_path / "fpl_bootstrap_seed.json"
    seed_path.write_text(json.dumps(_SAMPLE_SEED), encoding="utf-8")
    monkeypatch.setattr(admin, "_resolve_seed_path", lambda: seed_path)
    monkeypatch.delenv("CRON_SECRET", raising=False)

    from fpl_intelligence.api.main import app

    def _override_db() -> Session:
        yield sl()

    app.dependency_overrides[deps._get_db_session] = _override_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_migration_adds_column_and_backfills_codes(client: TestClient) -> None:
    resp = client.post("/api/v1/admin/migrate-fpl-code")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["column_added"] is True
    assert body["players"] == 2
    assert body["players_with_code"] == 2

    # The players endpoint must now expose photo-ready FPL element codes.
    rows = client.get("/api/v1/players").json()
    by_name = {p["web_name"]: p for p in rows}
    assert by_name["M. Salah"]["code"] == 308
    assert by_name["Haaland"]["code"] == 309


def test_second_call_seals_endpoint(client: TestClient) -> None:
    first = client.post("/api/v1/admin/migrate-fpl-code")
    assert first.status_code == 200
    second = client.post("/api/v1/admin/migrate-fpl-code")
    assert second.status_code == 410
    assert second.json()["ok"] is False


def test_fresh_schema_skips_alter(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sl = _make_sessionmaker(drop_code_column=False)
    monkeypatch.setattr(admin, "SessionLocal", sl)
    seed_path = tmp_path / "fpl_bootstrap_seed.json"
    seed_path.write_text(json.dumps(_SAMPLE_SEED), encoding="utf-8")
    monkeypatch.setattr(admin, "_resolve_seed_path", lambda: seed_path)

    from fpl_intelligence.api.main import app

    def _override_db() -> Session:
        yield sl()

    app.dependency_overrides[deps._get_db_session] = _override_db
    try:
        with TestClient(app) as client:
            resp = client.post("/api/v1/admin/migrate-fpl-code")
            assert resp.status_code == 200
            body = resp.json()
            assert body["column_added"] is False
            assert body["players_with_code"] == 2
    finally:
        app.dependency_overrides.clear()
