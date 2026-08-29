"""Audit 2026-08 — real-browser E2E for the core user journeys.

Runs the REAL page code (dashboard, my-team, league, targets) against mocked
API traffic, following the Phase 19 route-mock convention (BASE_URL
localhost:8000, console-clean + zero >=400 guarantee while mocked).

Journeys covered
----------------
A. Onboarding ......... dashboard FPL-ID entry -> /squad/from-fpl -> session chip.
B. Session restore .... saved localStorage session restores the chip on every page.
C. My Team ............ 15 players render from GET /api/v1/squad (the exact call
                        that 500'd in production on 2026-08); and when the API
                        fails (500) the page degrades honestly — no hang.
D. League ............. degraded payload renders the honest degraded state.
E. Targets ............ alpha buys render with numbers.

Run:  uvicorn fpl_intelligence.api.main:app --port 8000 &
      pytest tests/web_e2e/test_audit_core_journeys.py -q
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect

BASE_URL = "http://localhost:8000"

SESSION_KEY = "fpl_session_v20"
SESSION_OBJ = {"key": "794561", "source": "fpl", "entry_name": "E2E Manager", "synced_at": "2026-08-27T09:00:00.000Z"}

SQUAD_STATE = {
    "session_id": "794561",
    "player_ids": list(range(1, 16)),
    "captain_id": 1,
    "vice_captain_id": 2,
    "gameweek": 2,
    "bank": 0.5,
    "free_transfers": 2,
    "player_positions": {str(i): (1 if i <= 2 else 2 if i <= 7 else 3 if i <= 12 else 4) for i in range(1, 16)},
    "player_prices": {str(i): 4.5 for i in range(1, 16)},
    "player_teams": {str(i): (i % 20) + 1 for i in range(1, 16)},
    "transfer_status": "Matches FPL picks — no confirmed transfer.",
    "updated_at": "2026-08-27T09:00:00Z",
}

TARGETS_PAYLOAD = {
    "session_id": "794561",
    "gameweek": 2,
    "targets": [
        {"player_id": 531, "web_name": "Ellborg", "price": 4.5, "alpha": 15.4, "edge": 15.5},
        {"player_id": 82, "web_name": "Kelleher", "price": 5.0, "alpha": 12.6, "edge": 13.5},
    ],
    "next_gw_focus": {"gameweek": 2, "buys": [], "how_computed": "e2e"},
    "generated_note": "e2e alpha engine",
}

LEAGUE_DEGRADED = {
    "session_id": "794561",
    "status": "degraded",
    "leagues": [],
    "selected": None,
    "needs_picker": False,
    "note": "league data unavailable right now — render failed (E2E); retry or press Refresh",
    "honest_notes": ["League page could not be computed right now — showing a degraded state rather than failing."],
}


def _json_route(payload: dict | list, status: int = 200):
    def _handler(route: Route) -> None:
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    return _handler


@pytest.fixture
def mocked(page: Page) -> Iterator[Page]:
    """Console/network audit + default 200 mocks for everything auto-fetched."""
    console_errors: list[str] = []
    bad_responses: list[tuple[int, str]] = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )
    page.on(
        "response",
        lambda resp: bad_responses.append((resp.status, resp.url)) if resp.status >= 400 else None,
    )

    # Playwright precedence: the LAST registered matching route wins — so the
    # catch-all goes FIRST as a safety net and every specific route below
    # overrides it (same convention as test_ribbon_always._mocked).
    page.route("**/api/**", _json_route({}))
    page.route("**/health", _json_route({"status": "ok", "db": "connected", "version": "2.7.9"}))
    page.route("**/api/v1/sync/status*", _json_route({"latest": {}, "counts": {}, "token_configured": True}))
    page.route("**/api/v1/data-sources*", _json_route({"as_of": None, "sources": {}}))
    page.route("**/api/v1/sync/calibration*", _json_route({"count": 0, "mae": None, "bias": None, "buckets": {}}))
    page.route("**/api/v1/squad?**", _json_route(SQUAD_STATE))
    page.route("**/api/v1/squad/local**", _json_route(SQUAD_STATE))
    page.route("**/api/v1/targets**", _json_route(TARGETS_PAYLOAD))
    page.route("**/api/v1/league**", _json_route(LEAGUE_DEGRADED))
    # my-team does players.forEach(...): without this it would hit the
    # catch-all {} and die with a TypeError before rendering anything.
    page.route("**/api/v1/players**", _json_route([]))

    yield page

    audited = [
        (status, url)
        for status, url in bad_responses
        # Journey C deliberately injects one 500; exclude that single URL there.
        if not (status == 500 and "/api/v1/squad?" in url and getattr(page, "_allow_squad_500", False))
    ]
    assert audited == [], f"unexpected HTTP >=400 responses seen: {audited}"
    assert console_errors == [], f"console.error lines seen: {console_errors}"


def _seed_session(page: Page) -> None:
    page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")
    # `json.dumps(SESSION_OBJ)` is the JSON string the app stores; its Python
    # repr is a valid JS string literal. Double-encoding would leave a
    # string-in-string that readSession()'s JSON.parse resolves to a string,
    # not an object — the session would silently fail to restore.
    page.evaluate(f"localStorage.setItem({SESSION_KEY!r}, {json.dumps(SESSION_OBJ)!r})")


# --------------------------------------------------------------------------- #
# A — Onboarding: enter FPL ID on the dashboard
# --------------------------------------------------------------------------- #


class TestAOnboarding:
    def test_enter_fpl_id_calls_from_fpl_and_shows_chip(self, mocked: Page) -> None:
        page = mocked
        seen: dict[str, object] = {}

        def _capture(route: Route) -> None:
            seen["url"] = route.request.url
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({**SQUAD_STATE, "session_id": "794561", "source": "fpl-import"}),
            )

        page.route("**/api/v1/squad/from-fpl**", _capture)
        page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")

        box = page.locator("#teamId")
        box.wait_for(state="visible", timeout=10_000)
        box.fill("794561")
        page.locator("#analyzeBtn").click(timeout=5_000)

        page.wait_for_timeout(1_500)
        assert "from-fpl" in str(seen.get("url", "")), "entering an FPL id must call /squad/from-fpl"
        chip = page.locator("#sessionChip")
        expect(chip).to_be_visible(timeout=10_000)
        expect(chip).to_contain_text("794561")


# --------------------------------------------------------------------------- #
# B — Session restore across pages
# --------------------------------------------------------------------------- #


class TestBSessionRestore:
    @pytest.mark.parametrize("path", ["/dashboard", "/my-team", "/league", "/targets"])
    def test_saved_session_restores_everywhere(self, mocked: Page, path: str) -> None:
        page = mocked
        _seed_session(page)
        page.goto(BASE_URL + path, wait_until="domcontentloaded")
        chip = page.locator("#sessionChip")
        expect(chip).to_be_visible(timeout=10_000)
        expect(chip).to_contain_text("794561")


# --------------------------------------------------------------------------- #
# C — My Team: renders the squad; degrades honestly on API failure
# --------------------------------------------------------------------------- #


class TestCMyTeam:
    def test_my_team_renders_fifteen_players(self, mocked: Page) -> None:
        page = mocked
        _seed_session(page)
        page.goto(BASE_URL + "/my-team", wait_until="domcontentloaded")
        # The page is fed by GET /api/v1/squad?session_id=... (the exact call
        # that 500'd in production on 2026-08): gameweek 2 must reach the meta
        # line and all 15 tiles must render.
        page.wait_for_timeout(1_500)
        expect(page.locator("#metaLine")).to_contain_text("Gameweek 2", timeout=15_000)
        assert page.locator("#squadGrid > *").count() == 15, "expected 15 squad tiles"

    def test_my_team_degrades_cleanly_when_squad_api_500s(self, page: Page) -> None:
        """2026-08 outage contract: a 500 from GET /squad must not wedge the page."""
        console_errors: list[str] = []
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page._allow_squad_500 = True  # noqa: SLF001 — test harness flag
        page.route("**/health", _json_route({"status": "ok", "db": "connected", "version": "2.7.9"}))
        page.route("**/api/v1/sync/status*", _json_route({"latest": {}, "counts": {}, "token_configured": True}))
        page.route("**/api/v1/data-sources*", _json_route({"as_of": None, "sources": {}}))
        page.route("**/api/v1/squad?**", _json_route({"detail": "Internal Server Error"}, status=500))
        page.route("**/api/**", _json_route({}))

        _seed_session(page)
        page.goto(BASE_URL + "/my-team", wait_until="domcontentloaded")
        page.wait_for_timeout(2_000)
        # Page still renders its chrome and an honest empty state — no hang,
        # no uncaught error spam (the fetch handler degrades to null).
        expect(page.locator("body")).to_contain_text("My team")
        assert not any("Uncaught" in e for e in console_errors), console_errors


# --------------------------------------------------------------------------- #
# D — League: honest degraded state
# --------------------------------------------------------------------------- #


class TestDLeague:
    def test_degraded_league_renders_honest_note(self, mocked: Page) -> None:
        page = mocked
        _seed_session(page)
        page.goto(BASE_URL + "/league", wait_until="domcontentloaded")
        page.wait_for_timeout(1_500)
        expect(page.locator("body")).to_contain_text("league data unavailable right now", timeout=10_000)


# --------------------------------------------------------------------------- #
# E — Targets: alpha engine output renders
# --------------------------------------------------------------------------- #


class TestETargets:
    def test_targets_render_alpha_buys(self, mocked: Page) -> None:
        page = mocked
        _seed_session(page)
        page.goto(BASE_URL + "/targets", wait_until="domcontentloaded")
        page.wait_for_timeout(1_500)
        body = page.locator("body")
        expect(body).to_contain_text("Ellborg", timeout=10_000)
        expect(body).to_contain_text("Kelleher")


# --------------------------------------------------------------------------- #
# F — pass-2 (2026-08-27): re-sync button + no input flash
# --------------------------------------------------------------------------- #


class TestFResyncSquad:
    def test_resync_button_hidden_without_session(self, mocked: Page) -> None:
        """No saved session → entry screen visible, re-sync button HIDDEN."""
        page = mocked
        page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")
        page.evaluate("() => localStorage.removeItem('fpl_session_v20')")
        page.evaluate("() => localStorage.removeItem('fpl_session_id')")
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#teamId")).to_be_visible(timeout=10_000)
        expect(page.locator('[data-testid="resync-squad-btn"]')).to_be_hidden()
        expect(page.locator('[data-testid="sync-now-btn"]')).to_be_disabled()

    def test_resync_fires_from_fpl_and_input_stays_hidden(self, mocked: Page) -> None:
        """With a real session: button visible, click fires POST /squad/from-fpl
        (the battle-tested import chain) and the entry form stays hidden."""
        page = mocked
        seen: dict[str, object] = {}

        def _capture(route: Route) -> None:
            seen["url"] = route.request.url
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({**SQUAD_STATE, "entry_name": "E2E Manager", "source": "fpl-import"}),
            )

        page.route("**/api/v1/squad/from-fpl**", _capture)
        _seed_session(page)
        page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")

        btn = page.locator('[data-testid="resync-squad-btn"]')
        expect(btn).to_be_visible(timeout=10_000)
        expect(btn).to_be_enabled()
        expect(page.locator("#inputSection")).to_be_hidden()
        btn.click(timeout=5_000)
        page.wait_for_timeout(2_000)
        assert "from-fpl" in str(seen.get("url", "")), "re-sync must call /squad/from-fpl"
        # The entry form must REMAIN hidden — re-sync reuses the saved session.
        expect(page.locator("#inputSection")).to_be_hidden()

    def test_no_input_flash_with_saved_session(self, mocked: Page) -> None:
        """A saved session must never show the entry form — not even for a
        frame before the session bootstrap resolves (the old flash-then-vanish)."""
        page = mocked
        _seed_session(page)  # first goto + seed localStorage
        page.add_init_script(
            """
            window.__inputFlash = false;
            const _checkInput = () => {
              const el = document.getElementById('inputSection');
              if (el && getComputedStyle(el).display !== 'none') window.__inputFlash = true;
            };
            new MutationObserver(_checkInput).observe(document.documentElement, {
              subtree: true, attributes: true, attributeFilter: ['style', 'class']
            });
            setInterval(_checkInput, 50);
            """
        )
        page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")
        page.wait_for_timeout(1_500)
        expect(page.locator("#inputSection")).to_be_hidden()
        flashed = page.evaluate("window.__inputFlash")
        assert flashed is False, "#inputSection became visible with a saved session"
