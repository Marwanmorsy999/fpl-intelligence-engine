"""Hotfix v1.3.4 — admin seed-from-file endpoint tests.

The committed offline FPL bootstrap seed (data/seed/fpl_bootstrap_seed.json)
lets the deployed database get real teams + prices even though FPL blocks
Vercel's datacenter IPs. These tests prove POST /api/v1/admin/seed-from-file:

* populates PlayerTeamMembership + PlayerGameweekPerformance (price = now_cost/10);
* is idempotent (re-running creates no duplicate rows);
* makes GET /api/v1/players return non-null ``team`` and ``price``;
* honours CRON_SECRET (401 without it).

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
from fpl_intelligence.db.models import PlayerGameweekPerformance, PlayerTeamMembership

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


def test_seed_populates_memberships_and_performances(client: TestClient) -> None:
    resp = client.post("/api/v1/admin/seed-from-file")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["players"] == 2
    assert body["memberships_created"] == 2
    assert body["performances_created"] == 2

    db = admin.SessionLocal()
    try:
        memberships = db.query(PlayerTeamMembership).all()
        assert len(memberships) == 2
        prices = db.query(PlayerGameweekPerformance).all()
        assert len(prices) == 2
    finally:
        db.close()


def test_seed_is_idempotent(client: TestClient) -> None:
    client.post("/api/v1/admin/seed-from-file")
    body = client.post("/api/v1/admin/seed-from-file").json()
    assert body["memberships_created"] == 0
    assert body["performances_created"] == 0


def test_players_endpoint_returns_team_and_price(client: TestClient) -> None:
    client.post("/api/v1/admin/seed-from-file")
    rows = client.get("/api/v1/players").json()
    by_name = {p["web_name"]: p for p in rows}
    salah = by_name["M. Salah"]
    assert salah["team"] is not None
    assert salah["price"] == pytest.approx(13.0)  # now_cost 130 / 10
    haaland = by_name["Haaland"]
    assert haaland["team"] is not None
    assert haaland["price"] == pytest.approx(14.0)


def test_seed_requires_cron_secret(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", "secret")
    denied = client.get("/api/v1/admin/seed-from-file")
    assert denied.status_code == 401
    ok = client.get("/api/v1/admin/seed-from-file", params={"secret": "secret"})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True