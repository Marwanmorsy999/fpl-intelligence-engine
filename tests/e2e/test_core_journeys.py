"""End-to-end core-journey suite (audit 2026-08).

Hermetic (in-memory SQLite, no network): exercises each user journey from the
outside via the real ASGI app, exactly as the browser/bookmarklet/cron do.

Journeys
--------
J1  Land & trust .......... /health, / -> /dashboard redirect, /dashboard HTML.
J2  Session bootstrap ..... POST /squad -> GET /squad?mode=plan|fpl (the
                            2026-08 live-500 regression), local overlay truth.
J3  Decisions ............. GET /decisions is 200/404/503 — NEVER a bare 500.
J4  My Team fixtures ...... GET /fixtures/scan honest states, never a bare 500.
J5  League ................ GET /league honest states for junk/unknown/None
                            sessions (v2.7.6 regression), never a bare 500.
J6  Bookmarklet push ...... /sync/squad-push auth contract
                            (503 unconfigured / 401 bad bearer / 200 valid) and
                            persistence under the entry-id session key.
J7  Admin security ........ CRON_SECRET contract incl. the production
                            fail-closed hardening and constant-time bearer.
J8  Cache policy .......... personal endpoints are private/no-store; public
                            bootstrap reads are CDN-cacheable.
J9  Telegram webhook ...... wrong secret rejected without a 5xx.

Run:  pytest tests/e2e -q
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.squad.models import SquadStateCreate

# --------------------------------------------------------------------------- #
# Fixture: the real app on an isolated in-memory database
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_client(db_session: Session) -> Generator[tuple[TestClient, Session], None, None]:
    """TestClient over the production app with the DB dependency overridden."""
    from fpl_intelligence.api.main import app

    def _override() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[deps._get_db_session] = _override
    try:
        yield TestClient(app, raise_server_exceptions=False), db_session
    finally:
        app.dependency_overrides.pop(deps._get_db_session, None)


def _base_payload(gw: int = 2) -> SquadStateCreate:
    return SquadStateCreate(
        player_ids=list(range(1, 16)),
        captain_id=1,
        vice_captain_id=2,
        gameweek=gw,
        player_positions={i: (1 if i <= 2 else 2 if i <= 7 else 3 if i <= 12 else 4) for i in range(1, 16)},
        player_prices={i: 4.5 for i in range(1, 16)},
        bank=0.5,
    )


def _push_payload(entry_id: int = 2295006) -> dict[str, Any]:
    picks = []
    for pos in range(1, 16):
        picks.append(
            {
                "element_id": pos,
                "position": pos,
                "is_captain": pos == 1,
                "is_vice": pos == 2,
                "element_type": 1 if pos <= 2 else 2,
            }
        )
    return {
        "entry_id": entry_id,
        "entry_name": "E2E Manager",
        "gameweek": 2,
        "picks": picks,
        "bank": 5.0,
    }


# --------------------------------------------------------------------------- #
# J1 — Land & trust
# --------------------------------------------------------------------------- #


class TestJ1LandAndTrust:
    def test_health_ok(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in {"ok", "degraded"}
        assert "version" in body

    def test_root_redirects_to_dashboard(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in {301, 302, 307}
        assert resp.headers["location"].endswith("/dashboard")

    def test_dashboard_serves_html(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "FPL" in resp.text


# --------------------------------------------------------------------------- #
# J2 — Session bootstrap (manual squad save + truth modes)
# --------------------------------------------------------------------------- #


class TestJ2SessionBootstrap:
    def test_save_then_read_squad_roundtrip(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        saved = client.post(
            "/api/v1/squad", params={"session_id": "e2e_u1"}, json=_base_payload().model_dump(mode="json")
        )
        assert saved.status_code == 200, saved.text
        got = client.get("/api/v1/squad", params={"session_id": "e2e_u1"})
        # Regression (2026-08 live outage): this endpoint 500'd in production
        # because the route passed a ``mode`` kwarg the service never accepted.
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["player_ids"] == list(range(1, 16))
        assert body["captain_id"] == 1

    def test_mode_fpl_excludes_local_overlay(
        self, app_client: tuple[TestClient, Session]
    ) -> None:
        """Phase-2 truth: plan mode may show the planned player, fpl mode may not."""
        client, _ = app_client
        client.post(
            "/api/v1/squad", params={"session_id": "e2e_u2"}, json=_base_payload().model_dump(mode="json")
        )
        local = client.post(
            "/api/v1/squad/local",
            json={"session_id": "e2e_u2", "element_out": 15, "element_in": 115},
        )
        assert local.status_code == 200, local.text
        plan = client.get("/api/v1/squad", params={"session_id": "e2e_u2", "mode": "plan"})
        fpl = client.get("/api/v1/squad", params={"session_id": "e2e_u2", "mode": "fpl"})
        assert plan.status_code == 200 and fpl.status_code == 200
        assert 115 in plan.json()["player_ids"]
        assert 115 not in fpl.json()["player_ids"]

    def test_missing_and_unknown_session_404(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        assert client.get("/api/v1/squad").status_code == 404
        assert client.get("/api/v1/squad", params={"session_id": "ghost"}).status_code == 404

    def test_squad_responses_are_never_edge_cached(
        self, app_client: tuple[TestClient, Session]
    ) -> None:
        client, _ = app_client
        client.post(
            "/api/v1/squad", params={"session_id": "e2e_u3"}, json=_base_payload().model_dump(mode="json")
        )
        resp = client.get("/api/v1/squad", params={"session_id": "e2e_u3"})
        assert "no-store" in resp.headers.get("cache-control", "").lower()


# --------------------------------------------------------------------------- #
# J3 — Decisions
# --------------------------------------------------------------------------- #


class TestJ3Decisions:
    def test_unknown_session_is_honest_404(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        resp = client.get("/api/v1/decisions", params={"session_id": "ghost"})
        assert resp.status_code == 404

    def test_saved_session_never_bare_500(self, app_client: tuple[TestClient, Session]) -> None:
        """With a real squad the report is either complete (200) or an honest 503."""
        client, _ = app_client
        client.post(
            "/api/v1/squad", params={"session_id": "e2e_d1"}, json=_base_payload().model_dump(mode="json")
        )
        resp = client.get("/api/v1/decisions", params={"session_id": "e2e_d1"})
        assert resp.status_code in {200, 503}, f"unexpected {resp.status_code}: {resp.text[:300]}"
        if resp.status_code == 200:
            body = resp.json()
            xi = body.get("starting_xi") or body.get("startingXI") or []
                    # Skeleton guard: a populated squad must yield a non-empty XI.
            assert xi, f"empty starting XI in report: {list(body)[:12]}"


# --------------------------------------------------------------------------- #
# J4 — My Team fixtures feed
# --------------------------------------------------------------------------- #


class TestJ4MyTeamFixtures:
    def test_scan_requires_session(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        resp = client.get("/api/v1/fixtures/scan")
        assert resp.status_code == 404

    def test_scan_unknown_session_404(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        resp = client.get("/api/v1/fixtures/scan", params={"session_id": "ghost"})
        assert resp.status_code == 404

    def test_scan_known_session_honest_states(self, app_client: tuple[TestClient, Session]) -> None:
        """With no fixtures published the endpoint degrades to 503, never a bare 500."""
        client, _ = app_client
        client.post(
            "/api/v1/squad", params={"session_id": "e2e_f1"}, json=_base_payload().model_dump(mode="json")
        )
        resp = client.get("/api/v1/fixtures/scan", params={"session_id": "e2e_f1"})
        assert resp.status_code in {200, 503}, f"unexpected {resp.status_code}"
        if resp.status_code == 200:
            body = resp.json()
            assert isinstance(body["players"], list)
            assert len(body["players"]) == 15
            assert body["horizon_gws"]


# --------------------------------------------------------------------------- #
# J5 — League
# --------------------------------------------------------------------------- #


class TestJ5League:
    @pytest.mark.parametrize("bad", ["None", "", "abc", "12.3", "-1"])
    def test_junk_sessions_never_500(self, app_client: tuple[TestClient, Session], bad: str) -> None:
        client, _ = app_client
        resp = client.get("/api/v1/league", params={"session_id": bad})
        assert resp.status_code == 200  # degraded-but-200 by design
        body = resp.json()
        assert body.get("status") in {"no-league", "degraded", "stale"}
        assert body.get("leagues") == []

    def test_numeric_unknown_session_is_honest(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        resp = client.get("/api/v1/league", params={"session_id": "000000"})
        assert resp.status_code == 200
        assert resp.json().get("status") in {"no-league", "stale", "degraded", "ok"}


# --------------------------------------------------------------------------- #
# J6 — Bookmarklet / machine push auth contract
# --------------------------------------------------------------------------- #


class TestJ6SyncPush:
    def test_unconfigured_token_is_503(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        resp = client.post("/api/v1/sync/squad-push", json=_push_payload())
        assert resp.status_code == 503  # an unconfigured deployment accepts nothing

    def test_auth_contract_and_persistence(
        self, app_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fpl_intelligence.config import get_settings

        client, db = app_client
        monkeypatch.setattr(get_settings(), "sync_push_token", "e2e-push-token")
        payload = _push_payload(entry_id=2295006)

        no_auth = client.post("/api/v1/sync/squad-push", json=payload)
        assert no_auth.status_code == 401
        bad_auth = client.post(
            "/api/v1/sync/squad-push", json=payload, headers={"Authorization": "Bearer wrong"}
        )
        assert bad_auth.status_code == 401

        ok = client.post(
            "/api/v1/sync/squad-push", json=payload, headers={"Authorization": "Bearer e2e-push-token"}
        )
        assert ok.status_code == 200, ok.text

        # The pushed squad is readable under the entry-id session key.
        got = client.get("/api/v1/squad", params={"session_id": "2295006"})
        assert got.status_code == 200, got.text
        assert got.json()["player_ids"] == list(range(1, 16))

        from fpl_intelligence.sync.models import SyncLogDB

        assert db.query(SyncLogDB).count() >= 1


# --------------------------------------------------------------------------- #
# J7 — Admin / cron security
# --------------------------------------------------------------------------- #


class TestJ7AdminSecurity:
    def test_cron_secret_contract(self, app_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = app_client
        monkeypatch.setenv("CRON_SECRET", "e2e-cron-secret")
        monkeypatch.delenv("APP_ENV", raising=False)
        assert client.get("/api/v1/admin/db-probe").status_code == 401
        assert (
            client.get(
                "/api/v1/admin/db-probe", headers={"Authorization": "Bearer nope"}
            ).status_code
            == 401
        )
        ok = client.get(
            "/api/v1/admin/db-probe", headers={"Authorization": "Bearer e2e-cron-secret"}
        )
        assert ok.status_code == 200, ok.text

    def test_production_without_secret_fails_closed(
        self, app_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = app_client
        monkeypatch.delenv("CRON_SECRET", raising=False)
        monkeypatch.setenv("APP_ENV", "production")
        resp = client.get("/api/v1/admin/db-probe")
        assert resp.status_code == 503  # hardening: admin stays closed, not open

    def test_one_shot_admin_routes_require_auth_when_secret_set(
        self, app_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = app_client
        monkeypatch.setenv("CRON_SECRET", "e2e-cron-secret")
        monkeypatch.delenv("APP_ENV", raising=False)
        for path in ("/api/v1/admin/initialize-data", "/api/v1/admin/migrate-fpl-code"):
            resp = client.post(path)
            assert resp.status_code == 401, f"{path} should demand cron auth"

    def test_alembic_version_is_not_public(
        self, app_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = app_client
        monkeypatch.setenv("CRON_SECRET", "e2e-cron-secret")
        monkeypatch.delenv("APP_ENV", raising=False)
        assert client.get("/api/v1/admin/alembic-version").status_code == 401


# --------------------------------------------------------------------------- #
# J8 — Edge cache policy contract
# --------------------------------------------------------------------------- #


class TestJ8CachePolicy:
    def test_public_api_reads_are_cacheable(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        resp = client.get("/api/v1/players")
        assert resp.status_code == 200
        assert "no-store" not in resp.headers.get("cache-control", "").lower()

    def test_league_reads_are_private(self, app_client: tuple[TestClient, Session]) -> None:
        client, _ = app_client
        resp = client.get("/api/v1/league", params={"session_id": "123"})
        assert "no-store" in resp.headers.get("cache-control", "").lower()


# --------------------------------------------------------------------------- #
# J9 — Telegram webhook
# --------------------------------------------------------------------------- #


class TestJ9TelegramWebhook:
    def test_wrong_secret_rejected_without_5xx(
        self, app_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = app_client
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "e2e-tg-secret")
        resp = client.post("/api/v1/telegram/webhook?secret=wrong", json={"update_id": 1})
        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "error": "secret mismatch"}

    def test_production_without_secret_fails_closed(
        self, app_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = app_client
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        resp = client.post("/api/v1/telegram/webhook", json={"update_id": 1})
        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "error": "webhook secret not configured"}
