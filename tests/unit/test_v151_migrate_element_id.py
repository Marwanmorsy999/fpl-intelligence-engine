"""v1.5.1 — one-shot migrate-fpl-element-id endpoint tests.

POST /api/v1/admin/migrate-fpl-element-id applies migration 0016
(``players.fpl_element_id``) to a database created before it and backfills every
player's official FPL element id from the external-id mapping + seed. These
tests prove it:

* adds the missing column when the schema predates migration 0016;
* backfills element ids so squad imports resolve names correctly;
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
    "teams": [{"id": 1, "name": "Manchester City", "short_name": "MCI"}],
    "events": [{"id": 1, "name": "Gameweek 1"}],
    "players": [
        {
            "id": 411,
            "first_name": "Erling",
            "second_name": "Haaland",
            "web_name": "Haaland",
            "position": 4,
            "team": 1,
            "now_cost": 155,
            "code": 223094,
        },
        {
            "id": 12,
            "first_name": "Bukayo",
            "second_name": "Saka",
            "web_name": "Saka",
            "position": 3,
            "team": 1,
            "now_cost": 95,
            "code": 223340,
        },
    ],
}


def _make_sessionmaker(drop_element_column: bool) -> sessionmaker:
    """Create an app-schema SQLite DB, optionally simulating pre-0016 schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    if drop_element_column:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP INDEX ix_players_fpl_element_id")
            conn.exec_driver_sql("ALTER TABLE players DROP COLUMN fpl_element_id")
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture
def client(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
):
    sl = _make_sessionmaker(drop_element_column=True)
    monkeypatch.setattr(admin, "SessionLocal", sl)
    seed_path = tmp_path / "fpl_bootstrap_seed.json"
    seed_path.write_text(json.dumps(_SAMPLE_SEED), encoding="utf-8")
    monkeypatch.setattr(admin, "_resolve_seed_path", lambda: seed_path)
    monkeypatch.delenv("CRON_SECRET", raising=False)

    from fpl_intelligence.api.main import app

    def _override_db():
        yield sl()

    app.dependency_overrides[deps._get_db_session] = _override_db
    try:
        with TestClient(app) as test_client:
            yield test_client, sl
    finally:
        app.dependency_overrides.clear()


def test_migration_adds_column_and_backfills_element_ids(client) -> None:
    test_client, _sl = client
    resp = test_client.post("/api/v1/admin/migrate-fpl-element-id")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["column_added"] is True
    assert body["players"] == 2
    assert body["players_with_element_id"] == 2


def test_second_call_seals_endpoint(client) -> None:
    test_client, _sl = client
    first = test_client.post("/api/v1/admin/migrate-fpl-element-id")
    assert first.status_code == 200
    second = test_client.post("/api/v1/admin/migrate-fpl-element-id")
    assert second.status_code == 410
    assert second.json()["ok"] is False


def test_fresh_schema_skips_alter(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sl = _make_sessionmaker(drop_element_column=False)
    monkeypatch.setattr(admin, "SessionLocal", sl)
    seed_path = tmp_path / "fpl_bootstrap_seed.json"
    seed_path.write_text(json.dumps(_SAMPLE_SEED), encoding="utf-8")
    monkeypatch.setattr(admin, "_resolve_seed_path", lambda: seed_path)

    from fpl_intelligence.api.main import app

    def _override_db():
        yield sl()

    app.dependency_overrides[deps._get_db_session] = _override_db
    try:
        with TestClient(app) as test_client:
            resp = test_client.post("/api/v1/admin/migrate-fpl-element-id")
            assert resp.status_code == 200
            body = resp.json()
            assert body["column_added"] is False
            assert body["players_with_element_id"] == 2
    finally:
        app.dependency_overrides.clear()