"""Admin endpoint resilience tests — FPL 403/429 fallback to Phase 11.1 providers.

These run fully offline: the official FPL provider is forced to 403, the
Phase 11.1 connectors are replaced by fakes that return canned data, and the
admin route's ``SessionLocal`` is redirected to an in-memory SQLite database.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.api.routes import admin
from fpl_intelligence.data_providers.facts import PlayerFact, FactSource
from fpl_intelligence.data_providers.football_data_org import Competition, Match
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import RawRecord
from fpl_intelligence.live_intelligence.connectors.base import SourceConnector, SourceConnectorError


def _make_sessionmaker() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _http_403() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://fantasy.premierleague.com/api/bootstrap-static/")
    response = httpx.Response(403, request=request)
    return httpx.HTTPStatusError("403 Forbidden", request=request, response=response)


class FakeFPLProvider:
    """Constructor mirrors OfficialFPLDataProvider; bootstrap always 403s."""

    def __init__(self, *, base_url: str = "", timeout: float = 20.0, max_retries: int = 3) -> None:
        self.base_url = base_url

    def get_bootstrap_static(self):
        raise _http_403()

    def get_fixtures(self):
        raise _http_403()


class FakeApiFootball:
    def __init__(self, **_: object) -> None:
        self._enabled = True

    def is_enabled(self) -> bool:
        return self._enabled

    def fetch_fixtures_by_date(self, date: str) -> list[dict]:
        return [{"fixture": {"id": 99}, "teams": {"home": {"id": 1}, "away": {"id": 2}}}]

    def collect_player_facts(self, *, date: str | None = None, **_: object):
        return [
            PlayerFact(
                source=FactSource.API_FOOTBALL,
                name="Salah",
                fpl_player_id=411,
                api_football_player_id=900,
                status="start",
                is_starting=True,
                expected_minutes=90,
            )
        ]


class FakeFootballData:
    def __init__(self, **_: object) -> None:
        self._enabled = True

    def is_enabled(self) -> bool:
        return self._enabled

    def fetch_competitions(self) -> list[Competition]:
        return [Competition(id=2021, name="Premier League", code="PL", area="England")]

    def fetch_matches(self, competition_code: str | None = None) -> list[Match]:
        return [
            Match(
                id=555,
                utc_date="2026-08-20T14:00:00Z",
                status="SCHEDULED",
                home_team="Liverpool",
                away_team="Chelsea",
                competition_code="PL",
            )
        ]


class FakeFPLConnector(SourceConnector):
    """Stand-in for FPLAPIConnector that always fails (simulating a block)."""

    name = "fpl_api"

    def fetch(self):  # pragma: no cover - exercised only in scheduler test
        raise SourceConnectorError("FPL API blocked (403)")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    SessionLocal = _make_sessionmaker()
    monkeypatch.setattr(admin, "SessionLocal", SessionLocal)
    monkeypatch.setattr(admin, "OfficialFPLDataProvider", FakeFPLProvider)
    monkeypatch.setattr(admin, "ApiFootballConnector", FakeApiFootball)
    monkeypatch.setattr(admin, "FootballDataOrgConnector", FakeFootballData)
    monkeypatch.delenv("CRON_SECRET", raising=False)

    from fpl_intelligence.api.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_ingest_fpl_falls_back_on_403(client: TestClient) -> None:
    resp = client.post("/api/v1/admin/ingest-fpl")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fallback"] is True
    assert body["provider"] == "api_football+football_data_org"
    assert body["records"] > 0

    # The database must be populated from the fallback providers, not left empty.
    SessionLocal = admin.SessionLocal
    db = SessionLocal()
    try:
        sources = {row.source for row in db.query(RawRecord).all()}
        assert "api_football" in sources
        assert "football_data_org" in sources
    finally:
        db.close()


def test_ingest_fpl_fallback_logs_warning(client: TestClient, caplog) -> None:
    """The block must be logged with the exact fallback warning."""
    import logging

    logger = logging.getLogger("fpl_intelligence.api.routes.admin")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        client.post("/api/v1/admin/ingest-fpl")
    assert any(
        "FPL API blocked (403). Falling back to API-Football" in r.message
        for r in caplog.records
    )


def test_run_scheduler_falls_back_on_fpl_block(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the FPL news connector is blocked, the scheduler still succeeds."""
    monkeypatch.setattr(admin, "FPLAPIConnector", FakeFPLConnector)

    resp = client.post("/api/v1/admin/run-scheduler")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("fpl_blocked") is True
    # Fallback providers still contributed ingested items.
    assert body.get("fallback_items", 0) > 0
