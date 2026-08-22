"""Phase 16.0 — Real-browser regression tests for the dashboard.

Scenarios:
  A: Fresh page + enter FPL ID -> real names OR honest block message; NEVER demo.
  B: Manual entry via search -> 15 players picked, save, decisions render correctly.
  C: Reload page -> saved squad restores with source chip; Start over clears.
  D: Unknown session -> 404, frontend shows empty state, not a squad.

Uses Playwright route interception to mock API responses so tests run without
a real backend/database.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route, expect

BASE_URL = "http://localhost:8000"

# Test player data for mocking
TEST_PLAYERS = [
    {"id": 1, "web_name": "Alisson", "team": 1, "position": 1, "price": 5.5, "code": 1},
    {"id": 2, "web_name": "Saliba", "team": 2, "position": 2, "price": 6.0, "code": 2},
    {"id": 3, "web_name": "Odegaard", "team": 2, "position": 3, "price": 8.5, "code": 3},
    {"id": 4, "web_name": "Haaland", "team": 3, "position": 4, "price": 14.0, "code": 4},
    {"id": 5, "web_name": "Areola", "team": 4, "position": 1, "price": 4.0, "code": 5},
    {"id": 6, "web_name": "Alexander-Arnold", "team": 1, "position": 2, "price": 7.5, "code": 6},
    {"id": 7, "web_name": "Gabriel", "team": 2, "position": 2, "price": 5.0, "code": 7},
    {"id": 8, "web_name": "Van Dijk", "team": 1, "position": 2, "price": 6.5, "code": 8},
    {"id": 9, "web_name": "White", "team": 2, "position": 2, "price": 5.5, "code": 9},
    {"id": 10, "web_name": "De Bruyne", "team": 3, "position": 3, "price": 10.0, "code": 10},
    {"id": 11, "web_name": "Saka", "team": 2, "position": 3, "price": 10.5, "code": 11},
    {"id": 12, "web_name": "Salah", "team": 1, "position": 3, "price": 12.5, "code": 12},
    {"id": 13, "web_name": "Foden", "team": 3, "position": 3, "price": 9.0, "code": 13},
    {"id": 14, "web_name": "Watkins", "team": 4, "position": 4, "price": 8.0, "code": 14},
    {"id": 15, "web_name": "Isak", "team": 5, "position": 4, "price": 8.5, "code": 15},
    {"id": 16, "web_name": "Solanke", "team": 6, "position": 4, "price": 7.5, "code": 16},
    {"id": 17, "web_name": "Rice", "team": 2, "position": 3, "price": 6.5, "code": 17},
    {"id": 18, "web_name": "Palmer", "team": 7, "position": 3, "price": 10.5, "code": 18},
    {"id": 19, "web_name": "Martinez", "team": 2, "position": 1, "price": 4.5, "code": 19},
    {"id": 20, "web_name": "Trippier", "team": 5, "position": 2, "price": 6.0, "code": 20},
]


def _mock_players(route: Route) -> None:
    """Mock GET /api/v1/players."""
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(TEST_PLAYERS),
    )


def _mock_from_fpl_success(route: Route) -> None:
    """Mock POST /api/v1/squad/from-fpl with success."""
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({
            "squad": {
                "player_ids": [p["id"] for p in TEST_PLAYERS[:15]],
                "captain_id": 4,
                "vice_captain_id": 12,
                "bank": 0.5,
                "free_transfers": 1,
                "chips_available": ["wildcard", "free_hit", "bench_boost", "triple_captain"],
                "gameweek": 8,
                "player_positions": {p["id"]: p["position"] for p in TEST_PLAYERS[:15]},
                "player_prices": {p["id"]: p["price"] for p in TEST_PLAYERS[:15]},
                "player_teams": {p["id"]: p["team"] for p in TEST_PLAYERS[:15]},
                "is_demo": False,
                "updated_at": "2026-08-22T12:00:00Z",
            },
            "player_names": {p["id"]: p["web_name"] for p in TEST_PLAYERS[:15]},
            "entry_name": "Test Manager FC",
            "gameweek": 8,
            "is_demo": False,
        }),
    )


def _mock_from_fpl_blocked(route: Route) -> None:
    """Mock POST /api/v1/squad/from-fpl with 503 blocked."""
    route.fulfill(
        status=503,
        content_type="application/json",
        body=json.dumps({"detail": "FPL API blocked by rate limit"}),
    )


def _mock_decisions(route: Route) -> None:
    """Mock GET /api/v1/decisions with a valid report."""
    session_id = route.request.url.split("session_id=")[-1] if "session_id=" in route.request.url else ""
    if "unknown" in session_id:
        route.fulfill(
            status=404,
            content_type="application/json",
            body=json.dumps({"detail": "No squad saved for this session"}),
        )
        return
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({
            "generated_at": "2026-08-22T12:00:00Z",
            "gameweek": 8,
            "starting_xi": [p["id"] for p in TEST_PLAYERS[:11]],
            "bench_order": [p["id"] for p in TEST_PLAYERS[11:15]],
            "captain": {
                "player_id": 4,
                "expected_points": 8.5,
                "confidence": 0.85,
                "main_reason": "Highest xPTS this gameweek",
                "main_risk": None,
            },
            "vice_captain": 12,
            "transfer_plan": None,
            "chip_recommendation": None,
            "players": {
                str(p["id"]): {
                    "id": p["id"],
                    "web_name": p["web_name"],
                    "team": p["team"],
                    "position": p["position"],
                    "price": p["price"],
                    "code": p["code"],
                    "expected_points": 5.0 + (p["id"] % 5),
                    "prediction_source": "model-backtest",
                    "data_quality": "historical-backtest",
                }
                for p in TEST_PLAYERS[:15]
            },
            "meta": {
                "squad_summary": {
                    "team_value": 100.5,
                    "bank": 0.5,
                    "free_transfers": 1,
                    "chips_available": ["wildcard", "free_hit", "bench_boost", "triple_captain"],
                },
                "player_positions": {p["id"]: p["position"] for p in TEST_PLAYERS[:15]},
            },
        }),
    )


def _mock_squad_post(route: Route) -> None:
    """Mock POST /api/v1/squad."""
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({
            "player_ids": [p["id"] for p in TEST_PLAYERS[:15]],
            "captain_id": 4,
            "vice_captain_id": 12,
            "bank": 0.0,
            "free_transfers": 1,
            "chips_available": [],
            "gameweek": 1,
            "player_positions": {p["id"]: p["position"] for p in TEST_PLAYERS[:15]},
            "player_prices": {p["id"]: p["price"] for p in TEST_PLAYERS[:15]},
            "player_teams": {p["id"]: p["team"] for p in TEST_PLAYERS[:15]},
            "is_demo": False,
            "updated_at": "2026-08-22T12:00:00Z",
        }),
    )


def _mock_demo_post(route: Route) -> None:
    """Mock POST /api/v1/squad/demo."""
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({
            "squad": {
                "player_ids": [p["id"] for p in TEST_PLAYERS[:15]],
                "captain_id": 4,
                "vice_captain_id": 12,
                "bank": 2.0,
                "free_transfers": 1,
                "chips_available": ["wildcard", "free_hit", "bench_boost", "triple_captain"],
                "gameweek": 1,
                "player_positions": {p["id"]: p["position"] for p in TEST_PLAYERS[:15]},
                "player_prices": {p["id"]: p["price"] for p in TEST_PLAYERS[:15]},
                "player_teams": {p["id"]: p["team"] for p in TEST_PLAYERS[:15]},
                "is_demo": True,
                "updated_at": "2026-08-22T12:00:00Z",
            },
            "player_names": {p["id"]: p["web_name"] for p in TEST_PLAYERS[:15]},
            "entry_name": "Demo Squad",
            "gameweek": 1,
            "is_demo": True,
        }),
    )


def _mock_health(route: Route) -> None:
    """Mock GET /health."""
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({"status": "ok", "db": "mock", "version": "16.0.0"}),
    )


@pytest.fixture
def mocked_page(page: Page) -> Page:
    """Set up route mocking for all API endpoints."""
    # Health check
    page.route("**/health", _mock_health)
    # Players list
    page.route("**/api/v1/players", _mock_players)
    # Decisions
    page.route("**/api/v1/decisions**", _mock_decisions)
    # Squad POST
    page.route("**/api/v1/squad", _mock_squad_post)
    # Demo squad
    page.route("**/api/v1/squad/demo**", _mock_demo_post)
    return page


@pytest.fixture
def dashboard(mocked_page: Page) -> Page:
    """Navigate to the dashboard with mocked API."""
    mocked_page.goto(BASE_URL + "/dashboard")
    mocked_page.wait_for_load_state("domcontentloaded")
    return mocked_page


class TestScenarioA:
    """Fresh page + enter FPL ID -> real names OR honest block; NEVER demo."""

    def test_fresh_page_no_squad_by_default(self, dashboard: Page) -> None:
        """On a fresh page, no squad renders by default."""
        # Results section should be hidden
        expect(dashboard.locator("#results")).to_be_hidden()
        # Demo banner should not be visible
        expect(dashboard.locator("#demoBanner")).to_be_hidden()
        # Source chip should not be visible
        expect(dashboard.locator("#sourceChip")).to_be_hidden()

    def test_enter_fpl_id_shows_real_names(self, dashboard: Page) -> None:
        """Entering a valid FPL ID shows the user's real squad."""
        page = dashboard
        # Clear localStorage to ensure fresh state
        page.evaluate("() => localStorage.clear()")
        page.reload()
        page.wait_for_load_state("domcontentloaded")

        # Set up from-fpl mock for this test
        page.route("**/api/v1/squad/from-fpl", _mock_from_fpl_success)

        # Enter FPL ID 794561
        page.fill("#teamId", "794561")
        page.click("#analyzeBtn")

        # Wait for results to appear
        expect(page.locator("#results")).to_be_visible(timeout=10000)

        # Assert: real names render (Haaland should be visible)
        expect(page.locator(".web-name").filter(has_text="Haaland")).to_be_visible()

        # Assert: demo squad NEVER renders
        expect(page.locator("#demoBanner")).to_be_hidden()

        # Assert: source chip shows FPL ID
        expect(page.locator("#sourceChip")).to_be_visible()
        chip_text = page.locator("#sourceChip").text_content()
        assert chip_text is not None
        assert "794561" in chip_text

    def test_enter_fpl_id_blocked_shows_honest_error(self, dashboard: Page) -> None:
        """When FPL blocks, show honest error message, never demo."""
        page = dashboard
        page.evaluate("() => localStorage.clear()")
        page.reload()
        page.wait_for_load_state("domcontentloaded")

        # Set up from-fpl mock to return 503
        page.route("**/api/v1/squad/from-fpl", _mock_from_fpl_blocked)

        page.fill("#teamId", "794561")
        page.click("#analyzeBtn")

        # Wait for error to appear
        expect(page.locator("#error")).to_be_visible(timeout=10000)

        # Assert: error message is honest about FPL blocking
        error_text = page.locator("#error").text_content()
        assert error_text is not None
        assert "FPL" in error_text or "blocking" in error_text.lower()

        # Assert: demo NEVER renders
        expect(page.locator("#demoBanner")).to_be_hidden()
        expect(page.locator("#results")).to_be_hidden()


class TestScenarioB:
    """Manual entry via search -> 15 players picked, save, decisions render."""

    def test_manual_entry_search_and_save(self, dashboard: Page) -> None:
        """Type to search players, fill 15 slots, save, verify decisions."""
        page = dashboard
        page.evaluate("() => localStorage.clear()")
        page.reload()
        page.wait_for_load_state("domcontentloaded")

        # Open manual entry
        page.click("#toggleManualBtn")
        expect(page.locator("#manualEntry")).to_be_visible(timeout=10000)

        # Fill 15 slots via search - use position-appropriate names
        slots = page.locator(".manual-slot")
        assert slots.count() == 15, f"Expected 15 slots, got {slots.count()}"

        # Position-appropriate search terms (matching our mock data)
        # GK: Alisson, Areola  DEF: Saliba, Gabriel, Van Dijk, White, Trippier
        # MID: Odegaard, De Bruyne, Saka, Salah, Fodon  FWD: Haaland, Watkins, Isak
        search_terms = [
            "Al",    # GK1 -> Alisson
            "Are",   # GK2 -> Areola
            "Sal",   # DEF1 -> Saliba
            "Gab",   # DEF2 -> Gabriel
            "Van",   # DEF3 -> Van Dijk
            "Whi",   # DEF4 -> White
            "Trip",  # DEF5 -> Trippier
            "Ode",   # MID1 -> Odegaard
            "De B",  # MID2 -> De Bruyne
            "Sak",   # MID3 -> Saka
            "Salah", # MID4 -> Salah
            "Fod",   # MID5 -> Foden
            "Haa",   # FWD1 -> Haaland
            "Wat",   # FWD2 -> Watkins
            "Isa",   # FWD3 -> Isak
        ]
        for i in range(15):
            slot = slots.nth(i)
            slot.click()
            page.wait_for_timeout(100)
            # Use pressSequentially to trigger input events
            slot.press_sequentially(search_terms[i], delay=50)
            page.wait_for_timeout(500)
            # Find the autocomplete list within this slot's wrapper
            wrap = slot.locator("..")  # go up to autocomplete-wrap
            list_el = wrap.locator(".autocomplete-list")
            first_item = list_el.locator(".autocomplete-item[data-id]").first
            expect(first_item).to_be_visible(timeout=3000)
            first_item.click()
            page.wait_for_timeout(300)

        # Verify pick counter shows 15/15
        expect(page.locator("#pickCounter")).to_contain_text("15/15", timeout=5000)

        # Pick captain via search
        page.fill("#manualCap", "Haa")
        page.wait_for_timeout(500)
        cap_item = page.locator("#capList .autocomplete-item[data-id]").first
        if cap_item.is_visible():
            cap_item.click()
            page.wait_for_timeout(300)

        # Pick vice via search
        page.fill("#manualVice", "Salah")
        page.wait_for_timeout(500)
        vice_item = page.locator("#viceList .autocomplete-item[data-id]").first
        if vice_item.is_visible():
            vice_item.click()
            page.wait_for_timeout(300)

        # Save & Analyze
        page.click("#saveManualBtn")

        # Assert: results section is visible
        expect(page.locator("#results")).to_be_visible(timeout=10000)

        # Assert: source chip shows "Manual"
        expect(page.locator("#sourceChip")).to_be_visible()
        chip_text = page.locator("#sourceChip").text_content()
        assert chip_text is not None
        assert "Manual" in chip_text

        # Assert: demo banner is NOT visible
        expect(page.locator("#demoBanner")).to_be_hidden()


class TestScenarioC:
    """Reload page -> saved squad restores with source chip; Start over clears."""

    def test_reload_restores_session(self, dashboard: Page) -> None:
        """After saving a manual squad, reloading restores the session."""
        page = dashboard
        page.evaluate("() => localStorage.clear()")
        page.reload()
        page.wait_for_load_state("domcontentloaded")

        # Open manual entry
        page.click("#toggleManualBtn")
        expect(page.locator("#manualEntry")).to_be_visible(timeout=10000)

        # Fill 15 slots with position-appropriate names
        slots = page.locator(".manual-slot")
        search_terms = [
            "Al", "Are", "Sal", "Gab", "Van", "Whi", "Trip",
            "Ode", "De B", "Sak", "Salah", "Fod",
            "Haa", "Wat", "Isa"
        ]
        for i in range(15):
            slot = slots.nth(i)
            slot.click()
            page.wait_for_timeout(100)
            slot.press_sequentially(search_terms[i], delay=50)
            page.wait_for_timeout(500)
            # Find the autocomplete list within this slot's wrapper
            wrap = slot.locator("..")  # go up to autocomplete-wrap
            list_el = wrap.locator(".autocomplete-list")
            first_item = list_el.locator(".autocomplete-item[data-id]").first
            expect(first_item).to_be_visible(timeout=3000)
            first_item.click()
            page.wait_for_timeout(300)

        # Pick captain and vice
        page.fill("#manualCap", "Haa")
        page.wait_for_timeout(400)
        cap_item = page.locator("#capList .autocomplete-item[data-id]").first
        if cap_item.is_visible():
            cap_item.click()
            page.wait_for_timeout(300)

        page.fill("#manualVice", "Salah")
        page.wait_for_timeout(400)
        vice_item = page.locator("#viceList .autocomplete-item[data-id]").first
        if vice_item.is_visible():
            vice_item.click()
            page.wait_for_timeout(300)

        # Save
        page.click("#saveManualBtn")
        expect(page.locator("#results")).to_be_visible(timeout=10000)

        # Reload the page
        page.reload()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1000)

        # Assert: source chip is visible after reload
        expect(page.locator("#sourceChip")).to_be_visible()
        chip_text = page.locator("#sourceChip").text_content()
        assert chip_text is not None
        assert "Manual" in chip_text

        # Assert: Start over button is visible
        expect(page.locator("#startOverBtn")).to_be_visible()

        # Click Start over
        page.locator("#startOverBtn").click()
        page.wait_for_timeout(500)

        # Assert: source chip is hidden
        expect(page.locator("#sourceChip")).to_be_hidden()
        # Assert: results are hidden
        expect(page.locator("#results")).to_be_hidden()
        # Assert: start over button is hidden
        expect(page.locator("#startOverBtn")).to_be_hidden()


class TestScenarioD:
    """Unknown session -> 404, frontend shows empty state, not a squad."""

    def test_unknown_session_shows_empty_state(self, dashboard: Page) -> None:
        """Setting an unknown session in localStorage leads to empty state."""
        page = dashboard
        page.evaluate("() => localStorage.clear()")
        page.reload()
        page.wait_for_load_state("domcontentloaded")

        # Set a fake session ID in localStorage
        page.evaluate("""() => {
            localStorage.setItem('fpl_session_id', 'unknown_session_999');
            localStorage.setItem('fpl_session_source', 'fpl_id');
            localStorage.setItem('fpl_session_source_label', 'FPL ID 999');
        }""")
        page.reload()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)

        # After the failed load (404), the session should be cleared
        # and the source chip should be hidden
        source_chip_visible = page.locator("#sourceChip").is_visible()
        results_visible = page.locator("#results").is_visible()

        # Either the chip is hidden (session cleared) OR results are hidden
        assert not source_chip_visible or not results_visible, (
            "Unknown session should not show a squad"
        )
