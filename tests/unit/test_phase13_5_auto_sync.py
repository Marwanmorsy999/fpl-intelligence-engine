"""Phase 13.5 (revised) — squad auto-sync tests.

Covers the auto-sync flow that requires NO new secrets and NO new GitHub
Actions / cron slot:

* ``POST /api/v1/squad/from-fpl`` failing with 503 persists the ``entry_id``
  with ``auto_sync=true``;
* the public, rate-limited ``POST /api/v1/squad/retry-sync`` retries it —
  success saves the squad + sends the (mocked) Telegram push, failure marks
  the queued row FAILED, and repeated calls are rate-limited (429);
* the existing ``POST /api/v1/admin/run-scheduler`` cron folds the sync in, so
  the daily pass syncs any queued squad without a new cron/secret.

Fully offline: all SQLAlchemy sessions (request + admin) share one in-memory
SQLite engine, the upstream FPL importer is faked, the Telegram sender is
mocked (AsyncMock), and the scheduler connectors are replaced with offline
fakes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.api import deps
from fpl_intelligence.api.routes import admin
from fpl_intelligence.config import get_settings
from fpl_intelligence.data_providers.facts import FactSource, PlayerFact
from fpl_intelligence.data_providers.football_data_org import Competition, Match
from fpl_intelligence.db.base import Base
from fpl_intelligence.live_intelligence.connectors.base import (
    SourceConnector,
    SourceConnectorError,
)
from fpl_intelligence.squad import fpl_import as fpl_import_mod
from fpl_intelligence.squad import sync as sync_mod
from fpl_intelligence.squad.fpl_import import FplApiUnavailable, FplImportResult
from fpl_intelligence.squad.models import SquadStateCreate
from fpl_intelligence.squad.models_db import PendingSyncDB


def _squad_create() -> SquadStateCreate:
    return SquadStateCreate(
        player_ids=list(range(1, 16)),
        captain_id=3,
        vice_captain_id=11,
        bank=0.5,
        free_transfers=1,
        chips_available=["wildcard", "free_hit", "bench_boost", "triple_captain"],
        gameweek=8,
        player_positions={i: (i % 4) + 1 for i in range(1, 16)},
        player_prices={i: 5.0 for i in range(1, 16)},
        player_teams={i: 1 for i in range(1, 16)},
    )


def _canned_result(entry_name: str = "Test FC", gameweek: int = 8) -> FplImportResult:
    return FplImportResult(
        squad=_squad_create(),
        player_names={i: f"Player{i}" for i in range(1, 16)},
        entry_name=entry_name,
        gameweek=gameweek,
    )


class FakeImporter:
    """Stand-in for FplSquadImporter; returns a canned result or raises."""

    def __init__(
        self, result: FplImportResult | None = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error

    async def build_squad_from_entry(
        self, entry_id: int, db: Session | None = None
    ) -> FplImportResult:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class FakeFPLConnector(SourceConnector):
    """Always-failing FPL connector so the scheduler runs offline."""

    name = "fpl_api"

    def fetch(self):  # pragma: no cover - exercised only inside the scheduler
        raise SourceConnectorError("FPL API blocked (403)")


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


@pytest.fixture
def sl() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, sl: sessionmaker) -> TestClient:
    monkeypatch.setattr(admin, "SessionLocal", sl)
    monkeypatch.setattr(admin, "FPLAPIConnector", FakeFPLConnector)
    monkeypatch.setattr(admin, "ApiFootballConnector", FakeApiFootball)
    monkeypatch.setattr(admin, "FootballDataOrgConnector", FakeFootballData)
    monkeypatch.delenv("CRON_SECRET", raising=False)

    # Isolate the in-memory rate limiter between tests.
    from fpl_intelligence.api.routes import squad as squad_mod

    squad_mod._retry_sync_stamps.clear()

    from fpl_intelligence.api.main import app

    def _override() -> Session:
        db = sl()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[deps._get_db_session] = _override
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(sl.kw["bind"])


def _seed_pending(sl: sessionmaker, entry_id: int = 777) -> None:
    db = sl()
    try:
        db.add(
            PendingSyncDB(
                entry_id=entry_id,
                auto_sync=True,
                status="PENDING",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        db.commit()
    finally:
        db.close()


def _pending_rows(sl: sessionmaker) -> list[PendingSyncDB]:
    db = sl()
    try:
        return db.query(PendingSyncDB).all()
    finally:
        db.close()


def test_from_fpl_503_persists_auto_sync(
    client: TestClient, sl: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(entry_id, db=None):
        raise FplApiUnavailable("down")

    monkeypatch.setattr(
        fpl_import_mod.FplSquadImporter, "build_squad_from_entry", staticmethod(fake)
    )

    resp = client.post("/api/v1/squad/from-fpl", json={"entry_id": 777})
    assert resp.status_code == 503

    rows = _pending_rows(sl)
    assert len(rows) == 1
    assert rows[0].entry_id == 777
    assert rows[0].auto_sync is True
    assert rows[0].status == "PENDING"


def test_retry_sync_success_saves_and_notifies(
    client: TestClient,
    sl: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_pending(sl)
    monkeypatch.setattr(
        sync_mod,
        "FplSquadImporter",
        lambda *a, **k: FakeImporter(result=_canned_result(entry_name="Test FC")),
    )
    notifier = AsyncMock(return_value=True)
    monkeypatch.setattr(sync_mod, "send_squad_synced_notification", notifier)

    resp = client.post("/api/v1/squad/retry-sync")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entry_name"] == "Test FC"
    assert body["squad"]["gameweek"] == 8
    assert len(body["squad"]["player_ids"]) == 15

    # The squad is persisted and the queued row is SYNCED.
    resp_squad = client.get("/api/v1/squad", params={"session_id": "777"})
    assert resp_squad.status_code == 200
    rows = _pending_rows(sl)
    assert rows[0].status == "SYNCED"
    notifier.assert_awaited_once_with("Test FC")


def test_retry_sync_no_pending_returns_404(client: TestClient) -> None:
    resp = client.post("/api/v1/squad/retry-sync")
    assert resp.status_code == 404


def test_retry_sync_failure_marks_failed(
    client: TestClient,
    sl: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_pending(sl)
    monkeypatch.setattr(
        sync_mod,
        "FplSquadImporter",
        lambda *a, **k: FakeImporter(error=FplApiUnavailable("still down")),
    )
    notifier = AsyncMock()
    monkeypatch.setattr(sync_mod, "send_squad_synced_notification", notifier)

    resp = client.post("/api/v1/squad/retry-sync")
    assert resp.status_code == 503
    rows = _pending_rows(sl)
    assert rows[0].status == "FAILED"
    assert notifier.await_count == 0


def test_retry_sync_is_rate_limited(
    client: TestClient,
    sl: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_pending(sl)
    settings = get_settings()
    monkeypatch.setattr(settings, "retry_sync_rate_limit", 0)
    monkeypatch.setattr(settings, "retry_sync_rate_window_seconds", 60)

    resp = client.post("/api/v1/squad/retry-sync")
    assert resp.status_code == 429


def test_scheduler_syncs_pending_squad(
    client: TestClient,
    sl: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_pending(sl)
    monkeypatch.setattr(
        sync_mod,
        "FplSquadImporter",
        lambda *a, **k: FakeImporter(result=_canned_result(entry_name="Sched FC")),
    )
    notifier = AsyncMock(return_value=True)
    monkeypatch.setattr(sync_mod, "send_squad_synced_notification", notifier)

    resp = client.post("/api/v1/admin/run-scheduler")
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_sync"]["queued"] is True
    assert body["auto_sync"]["synced"] is True

    # Squad persisted, pending row cleared to SYNCED, notification sent.
    resp_squad = client.get("/api/v1/squad", params={"session_id": "777"})
    assert resp_squad.status_code == 200
    rows = _pending_rows(sl)
    assert rows[0].status == "SYNCED"
    notifier.assert_awaited_once_with("Sched FC")


def test_scheduler_skips_when_no_pending(client: TestClient) -> None:
    resp = client.post("/api/v1/admin/run-scheduler")
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_sync"]["queued"] is False
    assert body["auto_sync"]["synced"] is False
