"""v2.5.5 — ribbon ALWAYS visible: demo/no-session disabled + hint, fpl_id enabled."""
from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect

BASE_URL = "http://localhost:8000"

DECISIONS_MIN = {
    "generated_at": "2026-08-22T09:00:00",
    "gameweek": 2,
    "starting_xi": [1, 2, 3],
    "bench_order": [],
    "captain": {"player_id": 3, "expected_points": 7.4, "expected_gain": 1.2, "probability_positive": 0.7, "confidence": 0.8, "main_reason": "ok", "main_risk": ""},
    "vice_captain": 2,
    "transfer_plan": None,
    "chip_recommendation": None,
    "players": {
        "1": {"id": 1, "web_name": "Areola", "team": 9, "position": 1, "price": 5.5, "code": 1, "expected_points": 4.0, "prediction_source": "p", "minutes_estimate": 90, "start_prob": 0.95},
        "2": {"id": 2, "web_name": "Saliba", "team": 8, "position": 2, "price": 6.5, "code": 2, "expected_points": 4.8, "prediction_source": "p", "minutes_estimate": 90, "start_prob": 0.95},
        "3": {"id": 3, "web_name": "Haaland", "team": 13, "position": 4, "price": 14.0, "code": 3, "expected_points": 7.4, "prediction_source": "p", "minutes_estimate": 90, "start_prob": 0.95},
    },
    "meta": {"squad_summary": {"team_value": 26.0, "bank": 2.0, "free_transfers": 1, "chips_available": []}, "chain": {"source_label": "p", "data_quality": "ok"}},
}


def _json_route(payload: dict | list, status: int = 200):
    def _handler(route: Route) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
    return _handler


@pytest.fixture
def _mocked(page: Page) -> Iterator[Page]:
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    # safety net first
    page.route("**/api/**", _json_route({}))
    page.route("**/health", _json_route({"status": "ok", "db": "ok", "version": "2.5.5"}))
    page.route("**/api/v1/decisions*", _json_route(DECISIONS_MIN))
    page.route("**/api/v1/squad?*", _json_route({"player_ids": [1,2,3], "captain_id": 3, "vice_captain_id": 2, "bank": 2.0, "gameweek": 2}))
    page.route("**/api/v1/fixtures/scan*", _json_route({"players": []}))
    page.route("**/api/v1/news/radar*", _json_route({"news_flags": []}))
    page.route("**/api/v1/assistant/brief*", _json_route({"sections": {}}))
    page.route("**/api/v1/data-sources*", _json_route({"as_of": None, "sources": {}}))
    page.route("**/api/v1/players**", _json_route([{"id": 1, "fpl_element_id": 1, "web_name": "Areola"}]))
    page.route("**/api/v1/sync/**", _json_route({}))
    page.route("**/api/v1/league**", _json_route({"your_rank": None}))
    page.route("**/api/v1/transfers/**", _json_route({"transfers": []}))
    page.route("**/api/v1/targets**", _json_route({"targets": []}))
    page.route("**/api/v1/planner**", _json_route({"status": "no-squad"}))
    yield page
    assert errors == [], f"console.error: {errors}"


def _seed(page: Page, sess: dict | None):
    if sess is None:
        page.evaluate("() => localStorage.removeItem('fpl_session_v20')")
        page.evaluate("() => localStorage.removeItem('fpl_session_id')")
    else:
        page.evaluate("(s) => localStorage.setItem('fpl_session_v20', JSON.stringify(s))", sess)


class TestRibbonAlwaysVisible:
    def test_dashboard_ribbon_visible_no_session_disabled_hint(self, _mocked: Page):
        page = _mocked
        page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")
        _seed(page, None)
        page.reload(wait_until="domcontentloaded")
        ribbon = page.locator('[data-testid="sync-ribbon"]')
        expect(ribbon).to_be_visible(timeout=8000)
        btn = page.locator('[data-testid="sync-now-btn"]')
        expect(btn).to_be_visible()
        expect(btn).to_be_disabled()
        hint = page.locator('[data-testid="sync-ribbon-hint"]')
        expect(hint).to_be_visible()
        expect(hint).to_contain_text("Analyze your team to enable Refresh League Data")
        tog = page.locator('[data-testid="sync-next-gw-toggle"]')
        expect(tog).to_be_disabled()

    def test_dashboard_ribbon_visible_demo_disabled_hint(self, _mocked: Page):
        page = _mocked
        sess = {"key": "demo_12345", "source": "demo", "entry_name": "Demo Squad", "synced_at": "2026-08-22T09:00:00Z"}
        page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")
        _seed(page, sess)
        page.reload(wait_until="domcontentloaded")
        ribbon = page.locator('[data-testid="sync-ribbon"]')
        expect(ribbon).to_be_visible(timeout=8000)
        btn = page.locator('[data-testid="sync-now-btn"]')
        expect(btn).to_be_visible()
        expect(btn).to_be_disabled()
        hint = page.locator('[data-testid="sync-ribbon-hint"]')
        expect(hint).to_be_visible()
        expect(hint).to_contain_text("Analyze your team to enable Refresh League Data")

    def test_dashboard_ribbon_visible_fpl_id_enabled(self, _mocked: Page):
        page = _mocked
        sess = {"key": "794561", "source": "fpl_id", "entry_name": "Test FC", "synced_at": "2026-08-22T09:00:00Z"}
        page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")
        _seed(page, sess)
        page.reload(wait_until="domcontentloaded")
        ribbon = page.locator('[data-testid="sync-ribbon"]')
        expect(ribbon).to_be_visible(timeout=8000)
        btn = page.locator('[data-testid="sync-now-btn"]')
        expect(btn).to_be_visible()
        expect(btn).to_be_enabled()
        hint = page.locator('[data-testid="sync-ribbon-hint"]')
        expect(hint).to_be_hidden()
        tog = page.locator('[data-testid="sync-next-gw-toggle"]')
        expect(tog).to_be_enabled()

    def test_connect_ribbon_visible_demo_and_fpl_id(self, _mocked: Page):
        page = _mocked
        # demo on /connect
        sess_demo = {"key": "demo_999", "source": "demo", "entry_name": "Demo Squad", "synced_at": "2026-08-22T09:00:00Z"}
        page.goto(BASE_URL + "/connect", wait_until="domcontentloaded")
        _seed(page, sess_demo)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator('[data-testid="sync-ribbon"]')).to_be_visible(timeout=8000)
        expect(page.locator('[data-testid="sync-now-btn"]')).to_be_disabled()
        expect(page.locator('[data-testid="sync-ribbon-hint"]')).to_contain_text("Analyze your team to enable Refresh League Data")
        # fpl_id on /connect
        sess_real = {"key": "794561", "source": "fpl_id", "entry_name": "Test FC", "synced_at": "2026-08-22T09:00:00Z"}
        _seed(page, sess_real)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator('[data-testid="sync-ribbon"]')).to_be_visible(timeout=8000)
        expect(page.locator('[data-testid="sync-now-btn"]')).to_be_enabled()
        expect(page.locator('[data-testid="sync-ribbon-hint"]')).to_be_hidden()
