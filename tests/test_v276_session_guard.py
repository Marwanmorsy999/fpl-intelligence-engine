"""v2.7.6-session-guard — prevent int(\"None\") crash.

stored_entry_leagues() and every route casting session_id to int must degrade
to an empty list / honest no-league payload instead of raising ValueError.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.leagues.service import stored_entry_leagues

# ------------------------------------------------------------------ #
# Unit: stored_entry_leagues guard                                   #
# ------------------------------------------------------------------ #


class TestStoredEntryLeaguesGuard:
    """The string 'None' must return [] rather than crashing on int('None')."""

    def test_none_string_returns_empty(self, db_session: Session) -> None:
        assert stored_entry_leagues(db_session, "None") == []  # type: ignore[arg-type]

    def test_empty_string_returns_empty(self, db_session: Session) -> None:
        assert stored_entry_leagues(db_session, "") == []  # type: ignore[arg-type]

    def test_garbage_returns_empty(self, db_session: Session) -> None:
        assert stored_entry_leagues(db_session, "abc") == []  # type: ignore[arg-type]
        assert stored_entry_leagues(db_session, "12.3") == []  # type: ignore[arg-type]
        assert stored_entry_leagues(db_session, "-1") == []  # type: ignore[arg-type]

    def test_none_value_returns_empty(self, db_session: Session) -> None:
        assert stored_entry_leagues(db_session, None) == []  # type: ignore[arg-type]

    def test_valid_digits_still_queries(self, db_session: Session) -> None:
        # No rows seeded, so a numeric id simply returns [] from the query
        # rather than from the guard — exercises the happy int() path.
        assert stored_entry_leagues(db_session, "2295006") == []  # type: ignore[arg-type]
        assert stored_entry_leagues(db_session, 2295006) == []


# ------------------------------------------------------------------ #
# Route: GET /league?session_id=None must never 500                  #
# ------------------------------------------------------------------ #


class TestLeagueRoutesGuard:
    """GET /league and POST /league/refresh degrade honestly for 'None'."""

    def test_league_none_session_never_500(self, db_session: Session) -> None:
        from fpl_intelligence.api.main import app

        def _override() -> Generator[Session, None, None]:
            yield db_session

        app.dependency_overrides[deps._get_db_session] = _override
        try:
            client = TestClient(app)
            r = client.get("/api/v1/league", params={"session_id": "None"})
            assert r.status_code == 200, r.text[:600]
            body = r.json()
            assert body.get("leagues") == [], body
            # Never surfaces as a degraded 500 — either no-league or degraded
            assert body.get("status") in {"no-league", "degraded", "stale"}
        finally:
            app.dependency_overrides.pop(deps._get_db_session, None)

    def test_league_refresh_none_session_is_no_league(self, db_session: Session) -> None:
        from fpl_intelligence.api.main import app

        def _override() -> Generator[Session, None, None]:
            yield db_session

        app.dependency_overrides[deps._get_db_session] = _override
        try:
            client = TestClient(app)
            r = client.post(
                "/api/v1/league/refresh",
                json={"session_id": "None"},
            )
            assert r.status_code == 200, r.text[:600]
            body = r.json()
            assert body.get("status") in {"no-league", "error"}, body
            assert "Traceback" not in r.text
        finally:
            app.dependency_overrides.pop(deps._get_db_session, None)


# ------------------------------------------------------------------ #
# Route: GET /live?session_id=None skips doomed picks fetch          #
# ------------------------------------------------------------------ #


class TestLiveGuard:
    """GET /live must not build /api/entry/None/... and must never 500."""

    def test_live_skips_doomed_picks_fetch_for_none(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fpl_intelligence.api.routes.live as live_mod
        from fpl_intelligence.api.main import app

        calls: list[str] = []

        class _FakeChain:
            winning_strategy = "fake"

            async def fetch(self, path: str, **_: object) -> dict:  # type: ignore[no-untyped-def]
                calls.append(path)
                if path == "/api/bootstrap-static/":
                    return {
                        "events": [{"id": 1, "finished": False}],
                        "elements": [],
                        "teams": [],
                        "element_types": [],
                    }
                # picks / live paths should not be hit with "None" — if they
                # are, return empty so the route can still finish.
                return {}

        def _fake_chain(_kind: str):  # type: ignore[no-untyped-def]
            return _FakeChain(), 60

        monkeypatch.setattr(live_mod, "_chain", _fake_chain)

        # Avoid ESPN network call during the test (_espn_strip is async).
        async def _fake_espn(*_: object, **__: object) -> tuple[list, None]:  # type: ignore[no-untyped-def]
            return [], None

        monkeypatch.setattr(live_mod, "_espn_strip", _fake_espn)  # type: ignore[attr-defined]

        async def _never_parse(*_: object, **__: object):  # type: ignore[no-untyped-def]
            return []

        # fixtures scanner not needed — no fixtures cached, live_mode is False.
        # Patch just enough to keep the route deterministic.
        try:
            import fpl_intelligence.fixtures.scanner as _fx

            monkeypatch.setattr(_fx, "parse_fixtures", lambda *_a, **_k: [])
        except Exception:
            pass

        def _override() -> Generator[Session, None, None]:
            yield db_session

        app.dependency_overrides[deps._get_db_session] = _override
        try:
            client = TestClient(app)
            r = client.get("/api/v1/live", params={"session_id": "None"})
            assert r.status_code == 200, r.text[:800]
            # The doomed entry URL must never have been fetched.
            assert not any("/api/entry/None/" in c for c in calls), calls
            assert not any("None" in c and "/picks/" in c for c in calls), calls
            body = r.json()
            # picks mask should be a failure, not a crash
            assert body.get("masks", {}).get("picks", {}).get("status") == "fail"
        finally:
            app.dependency_overrides.pop(deps._get_db_session, None)
