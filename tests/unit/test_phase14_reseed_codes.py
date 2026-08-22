"""Phase 14.0 hotfix 2 — reseed-fpl-codes endpoint tests.

POST /api/v1/admin/reseed-fpl-codes replays the bootstrap seed so every
player's ``fpl_code`` is backfilled from the (refreshed) committed seed.
These tests prove it:

* fills in codes for players that exist without one;
* corrects placeholder codes on existing rows;
* self-disables — a second call returns 410.

Fully offline: the admin ``SessionLocal`` is redirected to an in-memory SQLite
database and ``_resolve_seed_path`` is pointed at a temp seed file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.api import deps
from fpl_intelligence.api.routes import admin
from fpl_intelligence.db.base import Base
from fpl_intelligence.ingestion.fpl import _get_or_create_player

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
            "code": 223094,
        },
        {
            "id": 412,
            "first_name": "Erling",
            "second_name": "Haaland",
            "web_name": "Haaland",
            "position": 4,
            "team": 1,
            "now_cost": 140,
            "code": 94825,
        },
    ],
}


def _make_sessionmaker() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def _client_ctx(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    sl: sessionmaker,
) -> Iterator[TestClient]:
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


def test_reseed_fills_missing_codes(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sl = _make_sessionmaker()
    # Players exist already (created by the earlier seed) but WITHOUT codes.
    db = sl()
    _get_or_create_player(db, "411", "Mohamed", "Salah", "M. Salah", 3, None)
    _get_or_create_player(db, "412", "Erling", "Haaland", "Haaland", 4, None)
    db.commit()
    db.close()

    with _client_ctx(tmp_path, monkeypatch, sl) as client:
        resp = client.post("/api/v1/admin/reseed-fpl-codes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["players"] == 2
        assert body["players_with_code"] == 2

        rows = client.get("/api/v1/players").json()
        by_name = {p["web_name"]: p for p in rows}
        assert by_name["M. Salah"]["code"] == 223094
        assert by_name["Haaland"]["code"] == 94825


def test_reseed_corrects_placeholder_codes(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sl = _make_sessionmaker()
    db = sl()
    # Placeholder codes (code == id) from the original bad seed.
    _get_or_create_player(db, "411", "Mohamed", "Salah", "M. Salah", 3, 411)
    _get_or_create_player(db, "412", "Erling", "Haaland", "Haaland", 4, 412)
    db.commit()
    db.close()

    with _client_ctx(tmp_path, monkeypatch, sl) as client:
        resp = client.post("/api/v1/admin/reseed-fpl-codes")
        assert resp.status_code == 200
        rows = client.get("/api/v1/players").json()
        by_name = {p["web_name"]: p for p in rows}
        assert by_name["M. Salah"]["code"] == 223094


def test_second_reseed_call_seals_endpoint(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sl = _make_sessionmaker()
    with _client_ctx(tmp_path, monkeypatch, sl) as client:
        first = client.post("/api/v1/admin/reseed-fpl-codes")
        assert first.status_code == 200
        second = client.post("/api/v1/admin/reseed-fpl-codes")
        assert second.status_code == 410
        assert second.json()["ok"] is False
