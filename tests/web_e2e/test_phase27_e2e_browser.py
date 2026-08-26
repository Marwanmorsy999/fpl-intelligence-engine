"""Phase 27 — Transfer Desk + Trajectory + FOMO e2e.

Mocked at network layer; real page JS runs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect

BASE_URL = "http://localhost:8000"
SESSION = {"key": "794561", "source": "fpl_id", "entry_name": "Test FC", "synced_at": "2026-08-22T09:00:00Z"}
SQUAD = {
    "player_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    "captain_id": 3,
    "vice_captain_id": 2,
    "bank": 2.0,
    "free_transfers": 1,
    "chips_available": [],
    "gameweek": 2,
    "player_positions": {str(i): (1 if i == 1 else 2 if i <= 5 else 3 if i <= 10 else 4) for i in range(1, 16)},
    "player_prices": {str(i): 5.0 for i in range(1, 16)},
    "player_teams": {str(i): 1 for i in range(1, 16)},
    "session_id": SESSION["key"],
    "updated_at": "2026-08-22T09:00:00Z",
}

TARGETS = [
    {"player_id": 99, "web_name": "Saka", "position": "MID", "position_code": 3, "team": "ARS", "price": 8.5, "xpts": 7.4, "pos_avg": 3.2, "edge": 4.2, "own_p": 0.12, "alpha": 3.1, "volatility": 1.2, "affordability": "bank", "need_weight": 1.0, "user_owns": False, "fixture_strip": []},
    {"player_id": 100, "web_name": "Haaland2", "position": "FWD", "position_code": 4, "team": "MCI", "price": 14.0, "xpts": 8.0, "pos_avg": 4.0, "edge": 4.0, "own_p": 0.4, "alpha": 2.4, "volatility": 1.0, "affordability": "bank", "need_weight": 1.0, "user_owns": False, "fixture_strip": []},
]

VALUATION = {
    "session_id": SESSION["key"],
    "status": "ok",
    "valuation": {"element_in": 99, "element_out": 1, "free_transfers": 1, "horizon_gws": [2, 3, 4], "used_gws": [2, 3, 4], "gaps": [], "per_gw": [{"gw": 2, "xpts_in": 7.4, "xpts_out": 2.0, "delta": 5.4}, {"gw": 3, "xpts_in": 6.0, "xpts_out": 2.5, "delta": 3.5}, {"gw": 4, "xpts_in": 5.0, "xpts_out": 1.0, "delta": 4.0}], "gross_ev": 12.9, "hit_cost": 0, "net_ev": 12.9, "note": "EV over GWs [2,3,4]", "recommendation": "EXECUTE", "how_computed": "FT Valuation = SUM(xPTS_in - xPTS_out) over next 3 GWs - hit cost"},
    "hit_analysis": {"hit_cost": 0, "gross_gain": 12.9, "net_ev": 12.9, "recommendation": "EXECUTE", "chip_text": "Cost: 0 pts (free transfer). Projected 3-week gain: +12.9 pts. Net EV: +12.9. Recommendation: EXECUTE."},
    "how_computed": "FT Valuation = SUM(xPTS_in - xPTS_out) over next 3 GWs - hit cost",
}

SHADOW = {
    "session_id": SESSION["key"],
    "status": "ok",
    "staged": {"element_in": 99, "element_out": 1},
    "shadow": {"label": "STAGED - Not yet pushed to FPL", "shadow_ids": [99] + list(range(2, 16)), "metrics": {str(i): {"xpts": 3.0, "alpha": 0.5, "edge": 0.5, "own_p": 0.1} for i in range(1, 16)} | {"99": {"xpts": 7.4, "alpha": 3.1, "edge": 4.2, "own_p": 0.12}}, "valuation": VALUATION["valuation"], "xi_xpts_current": 30.0, "xi_xpts_shadow": 35.0, "xi_delta": 5.0, "gameweek": 2, "how_computed": "shadow"},
    "shadow_ids": [99] + list(range(2, 16)),
    "captain_delta": {"current_captain": 3, "shadow_captain": 99, "changed": True, "current_xpts": 7.4, "shadow_xpts": 7.4},
    "bank": 2.0,
    "free_transfers": 1,
}

# Hit with -4 scenario
VALUATION_HIT = {
    "session_id": SESSION["key"],
    "status": "ok",
    "valuation": {"element_in": 99, "element_out": 1, "free_transfers": 0, "horizon_gws": [2, 3, 4], "used_gws": [2, 3, 4], "gaps": [], "per_gw": [], "gross_ev": 9.0, "hit_cost": 4, "net_ev": 5.0, "note": "EV over GWs", "recommendation": "EXECUTE", "how_computed": "FT Valuation"},
    "hit_analysis": {"hit_cost": 4, "gross_gain": 9.0, "net_ev": 5.0, "recommendation": "EXECUTE", "chip_text": "Cost: -4 pts. Projected 3-week gain: +9 pts. Net EV: +5. Recommendation: EXECUTE."},
}

TRAJECTORY = {
    "status": "ok",
    "selected": {"league_id": 10, "name": "Test League"},
    "gameweek": 2,
    "horizon_gws": [3, 4, 5],
    "series": [
        {"entry_id": "794561", "label": "You", "is_you": True, "current_total": 100, "points": [100, 130, 160, 190], "per_gw": [30, 30, 30]},
        {"entry_id": "201", "label": "SuperBata", "is_you": False, "current_total": 114, "points": [114, 134, 154, 174], "per_gw": [20, 20, 20]},
        {"entry_id": "202", "label": "Rival2", "is_you": False, "current_total": 110, "points": [110, 130, 150, 170], "per_gw": [20, 20, 20]},
        {"entry_id": "203", "label": "Rival3", "is_you": False, "current_total": 105, "points": [105, 125, 145, 165], "per_gw": [20, 20, 20]},
    ],
    "ranks": [{"794561": 4, "201": 1, "202": 2, "203": 3}, {"794561": 2, "201": 1, "202": 3, "203": 4}, {"794561": 1, "201": 2, "202": 3, "203": 4}, {"794561": 1, "201": 2, "202": 3, "203": 4}],
    "insight": "You are 14 pts behind SuperBata. Projected to overtake by GW4 based on your Alpha targets vs his fixture difficulty.",
    "partial_note": "",
    "how_computed": "Cumulative = current total + SUM(xPTS of XI per horizon GW)",
}

FOMO = {
    "status": "ok",
    "gameweek": 2,
    "captain_regret": {"gameweek": 2, "recommended_captain": 99, "recommended_points": 12, "user_captain": 3, "user_points": 6, "delta": 12, "line": "You lost 12 pts by ignoring the captain recommendation. You captained #3 (6 pts) vs engine pick #99 (12 pts doubled).", "how_computed": "Captain regret = (engine captain actual pts x2) - (your captain actual pts x2)"},
    "alpha_capture": {"gameweek": 2, "graded_transfers": 4, "right": 3, "rate": 0.75, "line": "Alpha Capture Rate: 75% of graded transfer calls were right", "how_computed": "right/(right+wrong)"},
    "cost_line": "You lost 12 pts and 2 ranks by ignoring the Saka recommendation.",
    "how_computed": "Captain regret vs engine pick + Alpha capture rate",
}


def _json(payload, status=200):
    def _h(route: Route):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
    return _h


@pytest.fixture
def mocked(page: Page) -> Iterator[Page]:
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    # seed
    page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")
    page.evaluate("(s)=>localStorage.setItem('fpl_session_v20', JSON.stringify(s))", SESSION)
    # blanket
    page.route("**/api/**", _json({}))
    page.route("**/health", _json({"status": "ok", "db": "ok", "version": "2.7.0"}))
    page.route("**/api/v1/squad?*", _json(SQUAD))
    page.route("**/api/v1/decisions*", _json({"gameweek": 2, "starting_xi": [1, 2, 3], "bench_order": [], "captain": {"player_id": 3, "expected_points": 7.4}, "players": {}}))
    page.route("**/api/v1/targets?*", _json({"session_id": SESSION["key"], "gameweek": 2, "targets": TARGETS, "next_gw_focus": {"gameweek": 3, "buys": []}}))
    page.route("**/api/v1/targets/squad-metrics*", _json({"metrics": {}}))
    page.route("**/api/v1/fixtures/scan*", _json({"players": [], "horizon_gws": [3, 4, 5]}))
    page.route("**/api/v1/players**", _json([{"id": i, "fpl_element_id": i, "web_name": f"Player{i}"} for i in range(1, 20)] + [{"id": 99, "fpl_element_id": 99, "web_name": "Saka"}, {"id": 100, "fpl_element_id": 100, "web_name": "Haaland"}]))
    # Phase 27
    page.route("**/api/v1/transfers/valuation*", _json(VALUATION))
    page.route("**/api/v1/transfers/shadow*", _json(SHADOW))
    page.route("**/api/v1/transfers/execute", _json({"status": "executed", "message": "Transfer Executed on FPL", "clipboard": "IN: Saka, OUT: Player1", "fpl_url": "https://fantasy.premierleague.com/transfers", "staged": {"element_in": 99, "element_out": 1}}))
    page.route("**/api/v1/league/trajectory*", _json(TRAJECTORY))
    page.route("**/api/v1/league/fomo*", _json(FOMO))
    page.route("**/api/v1/league?*", _json({"status": "ok", "selected": {"league_id": 10, "name": "Test League"}, "leagues": [], "standings_top": [], "your_rank": 4, "cache_age_seconds": 60}))
    page.route("**/api/v1/sync/**", _json({}))
    page.route("**/api/v1/prices/**", _json({"chips": {}}))
    yield page
    assert console_errors == [], f"console.error seen: {console_errors}"


class TestTransferDesk:
    def test_drag_and_drop_staging_and_valuation(self, mocked: Page):
        page = mocked
        page.goto(BASE_URL + "/transfers", wait_until="domcontentloaded")
        # squad grid visible
        squad_tile = page.locator('[data-testid="transfer-tile"]').first
        expect(squad_tile).to_be_visible(timeout=8000)
        # click OUT
        squad_tile.click()
        # pick IN via search: type Saka
        page.fill('[data-testid="target-search"]', "Sak")
        # wait for search results
        opt = page.locator('[data-testid="target-option"]').first
        expect(opt).to_be_visible(timeout=5000)
        opt.click()
        # Stage
        btn = page.locator('[data-testid="stage-transfer-btn"]')
        expect(btn).to_be_enabled()
        btn.click()
        # Shadow label
        expect(page.locator('[data-testid="staged-label"]')).to_be_visible()
        expect(page.locator('[data-testid="staged-label"]')).to_contain_text("STAGED")
        # valuation box
        expect(page.locator('[data-testid="valuation-box"]')).to_be_visible()
        expect(page.locator('[data-testid="ft-valuation"]')).to_contain_text("FT Valuation")
        # hit chip for free transfer case should say free transfer
        expect(page.locator('[data-testid="hit-cost-chip"]')).to_contain_text("free transfer")
        # shadow metrics
        expect(page.locator('[data-testid="shadow-metrics"]')).to_be_visible()
        expect(page.locator('[data-testid="shadow-row"]').first).to_be_visible()
        expect(page.locator('[data-testid="captain-delta"]')).to_contain_text("Captaincy")

    def test_execute_shows_success(self, mocked: Page):
        page = mocked
        page.goto(BASE_URL + "/transfers", wait_until="domcontentloaded")
        page.locator('[data-testid="transfer-tile"]').first.click()
        page.fill('[data-testid="target-search"]', "Sak")
        expect(page.locator('[data-testid="target-option"]').first).to_be_visible(timeout=5000)
        page.locator('[data-testid="target-option"]').first.click()
        page.locator('[data-testid="stage-transfer-btn"]').click()
        expect(page.locator('[data-testid="staged-label"]')).to_be_visible(timeout=5000)
        page.locator('[data-testid="execute-transfer-btn"]').click()
        expect(page.locator('[data-testid="execute-success"]')).to_be_visible(timeout=5000)
        expect(page.locator('[data-testid="execute-success"]')).to_contain_text("Transfer Executed on FPL")

    def test_drag_drop_api(self, mocked: Page):
        page = mocked
        page.goto(BASE_URL + "/transfers", wait_until="domcontentloaded")
        source = page.locator('[data-testid="transfer-tile"]').first
        target = page.locator('[data-testid="out-slot"]')
        expect(source).to_be_visible(timeout=8000)
        expect(target).to_be_visible()
        source.drag_to(target)
        # After drop, out slot should be filled
        expect(target).to_contain_text("Player", timeout=3000)

    def test_hit_cost_chip_minus_four(self, mocked: Page):
        # override valuation mock to hit cost 4
        mocked.route("**/api/v1/transfers/valuation*", _json(VALUATION_HIT))
        mocked.route("**/api/v1/transfers/shadow*", _json(SHADOW))
        page = mocked
        page.goto(BASE_URL + "/transfers", wait_until="domcontentloaded")
        page.locator('[data-testid="transfer-tile"]').first.click()
        page.fill('[data-testid="target-search"]', "Sak")
        expect(page.locator('[data-testid="target-option"]').first).to_be_visible(timeout=5000)
        page.locator('[data-testid="target-option"]').first.click()
        page.locator('[data-testid="stage-transfer-btn"]').click()
        expect(page.locator('[data-testid="hit-cost-chip"]')).to_be_visible(timeout=5000)
        expect(page.locator('[data-testid="hit-cost-chip"]')).to_contain_text("Cost: -4 pts.")
        expect(page.locator('[data-testid="hit-cost-chip"]')).to_contain_text("Projected 3-week gain: +9 pts.")
        expect(page.locator('[data-testid="hit-cost-chip"]')).to_contain_text("Net EV: +5")
        expect(page.locator('[data-testid="hit-cost-chip"]')).to_contain_text("EXECUTE")


class TestLeagueTrajectory:
    def test_trajectory_chart_renders(self, mocked: Page):
        page = mocked
        page.goto(BASE_URL + "/league", wait_until="domcontentloaded")
        expect(page.locator('[data-testid="trajectory-section"]')).to_be_visible(timeout=8000)
        expect(page.locator('[data-testid="trajectory-chart"]')).to_be_visible()
        expect(page.locator('[data-testid="trajectory-svg"]')).to_be_visible()
        # insight line
        expect(page.locator('[data-testid="trajectory-insight"]')).to_contain_text("14 pts behind SuperBata")
        expect(page.locator('[data-testid="trajectory-insight"]')).to_contain_text("Projected to overtake by GW4")

    def test_fomo_math_asserts(self, mocked: Page):
        page = mocked
        page.goto(BASE_URL + "/league", wait_until="domcontentloaded")
        expect(page.locator('[data-testid="fomo-section"]')).to_be_visible(timeout=8000)
        # fomo content loaded via JS; check cost line
        expect(page.locator('[data-testid="fomo-cost-line"]').first).to_contain_text("You lost 12 pts", timeout=8000)
        # also on track-record
        page.goto(BASE_URL + "/track-record", wait_until="domcontentloaded")
        # track-record needs entryInput prefilled; our fixture seeds it via session chip but page uses entryInput fallback
        page.fill("#entryInput", SESSION["key"])
        page.click("#loadBtn")
        expect(page.locator('[data-testid="fomo-box"]')).to_be_visible(timeout=8000)
        # alpha capture rate 75%
        expect(page.locator('[data-testid="alpha-capture-rate"]')).to_contain_text("75%")
        expect(page.locator('[data-testid="cost-of-ignoring"]')).to_contain_text("You lost 12 pts")
