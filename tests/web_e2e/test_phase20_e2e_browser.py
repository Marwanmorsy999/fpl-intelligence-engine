"""Phase 20.0 — real-browser regression: coherent intelligence.

Scenarios (API traffic mocked at the network layer; the browser runs the REAL
page code against a locally served app):

* SESSION PERSISTENCE: sync once, then visit every page — the squad renders
  on each with ZERO typing; the header chip is present everywhere.
* DRAWER: clicking a player opens the drawer with form bars and the
  next-5 fixture strip.
* BRIEF: the assistant page renders all six sections naming real squad players.
* Console stays clean everywhere: zero console errors.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect

BASE_URL = "http://localhost:8000"

SESSION = {
    "key": "794561",
    "source": "fpl_id",
    "entry_name": "Test FC",
    "synced_at": "2026-08-22T09:00:00Z",
}

SQUAD_PLAYERS = {
    "1": {"id": 1, "web_name": "Areola", "team": 9, "position": 1, "price": 5.5, "code": 1},
    "2": {"id": 2, "web_name": "Saliba", "team": 8, "position": 2, "price": 6.5, "code": 2},
    "3": {"id": 3, "web_name": "Haaland", "team": 13, "position": 4, "price": 14.0, "code": 3},
}
ALL_IDS = [1, 2, 3]


def _player_detail(pid: int) -> dict:
    return SQUAD_PLAYERS[str(pid)] | {
        "expected_points": {1: 4.0, 2: 4.8, 3: 7.4}[pid],
        "prediction_source": "pre-season-proxy-v2",
        "data_quality": "heuristic-proxy-enriched",
        "minutes_estimate": 90.0,
        "start_prob": 0.95,
        "xg": None,
        "xa": None,
        "xpts_breakdown": None,
    }


def _decisions_payload() -> dict:
    return {
        "generated_at": "2026-08-22T09:00:00",
        "gameweek": 2,
        "starting_xi": ALL_IDS,
        "bench_order": [],
        "captain": {
            "player_id": 3, "expected_points": 14.8, "expected_gain": 0.0,
            "probability_positive": 0.5, "confidence": 0.7,
            "main_reason": "fixture swing favours him", "main_risk": "",
        },
        "vice_captain": 2,
        "transfer_plan": None,
        "chip_recommendation": None,
        "players": {str(pid): _player_detail(pid) for pid in ALL_IDS},
        "meta": {
            "squad_summary": {
                "team_value": 26.0, "bank": 2.0,
                "free_transfers": 1, "chips_available": [],
            },
            "chain": {"source_label": "Pre-season proxy v2", "data_quality": "heuristic-proxy"},
        },
    }


def _squad_payload() -> dict:
    return {
        "player_ids": ALL_IDS,
        "captain_id": 3,
        "vice_captain_id": 2,
        "bank": 2.0,
        "free_transfers": 1,
        "chips_available": [],
        "gameweek": 2,
        "player_positions": {1: 1, 2: 2, 3: 4},
        "player_prices": {1: 5.5, 2: 6.5, 3: 14.0},
        "player_teams": {1: 9, 2: 8, 3: 13},
        "is_demo": False,
        "session_id": SESSION["key"],
        "updated_at": "2026-08-22T09:00:00Z",
    }


RUNS = [
    {"gw": 3, "opponent_id": 19, "opponent": "WHU", "is_home": True, "difficulty": 2},
    {"gw": 4, "opponent_id": 9, "opponent": "EVE", "is_home": False, "difficulty": 3},
    {"gw": 5, "opponent_id": 17, "opponent": "SUN", "is_home": True, "difficulty": 2},
    {"gw": 6, "opponent_id": 12, "opponent": "LIV", "is_home": False, "difficulty": 5},
    {"gw": 7, "opponent_id": 10, "opponent": "TOT", "is_home": True, "difficulty": 3},
]


def _scan_payload() -> dict:
    return {
        "session_id": SESSION["key"],
        "gameweek": 2,
        "horizon_gws": [3, 4, 5, 6, 7],
        "players": [
            {"player_id": pid, "web_name": SQUAD_PLAYERS[str(pid)]["web_name"],
             "position": SQUAD_PLAYERS[str(pid)]["position"], "price": 5.0,
             "is_starter": True, "runs": RUNS, "avg_fdr": 3.0, "swing": 0.0}
            for pid in ALL_IDS
        ],
        "squad_swing_score": 1.5,
        "easiest_runs": [
            {"team_id": 3, "short_name": "BOU", "avg_fdr": 2.0, "runs": RUNS[:4]},
            {"team_id": 16, "short_name": "NFO", "avg_fdr": 2.25, "runs": RUNS[:4]},
        ],
        "scanned_at": "2026-08-22T09:00:00Z",
    }


def _drawer_payload(pid: int) -> dict:
    name = SQUAD_PLAYERS[str(pid)]["web_name"]
    return {
        "session_id": SESSION["key"],
        "gameweek": 2,
        "player": {
            "id": pid, "web_name": name, "full_name": f"First {name}",
            "team": 13, "position": 4, "price": 14.0, "status": "a",
            "minutes_played": 180, "selected_by_percent": "44.1",
            "cost_change_event": -1,
        },
        "expected_points": 7.4,
        "prediction_source": "pre-season-proxy-v2",
        "data_quality": "heuristic-proxy-enriched",
        "xpts_breakdown": {"base": 3.0, "xg_xa_term": 2.4, "market_term": 2.0, "weather_term": 0.0},
        "xg_per_90": 0.81,
        "xa_per_90": 0.22,
        "minutes_estimate": 90.0,
        "start_prob": 0.97,
        "form_bars": [{"gw": g, "points": p, "minutes": 90} for g, p in [(-1, 2), (0, 12), (1, 6)]],
        "fixture_runs": RUNS,
        "avg_fdr": 3.0,
        "news_flags": None,
        "aliases": [name.lower()],
        "generated_at": "2026-08-22T09:00:00Z",
    }


BRIEF_PAYLOAD = {
    "session_id": SESSION["key"],
    "gameweek": 2,
    "sections": {
        "SQUAD STATUS": "15 players loaded · bank £2.0m · 1 free transfer(s). "
                        "Predictions from Pre-season proxy v2.",
        "CAPTAIN": "Haaland captains with xPTS 14.8 ahead of Saliba 4.8.",
        "TRANSFERS": "Roll the free transfer.",
        "FIXTURE SWINGS": "Squad swing +1.5 (easy patch). "
                          "Easiest upcoming runs: BOU (avg FDR 2.0), NFO (avg FDR 2.3).",
        "NEWS FLAGS": "No BBC headlines matched your squad.",
        "LAST WEEK GRADE": "No graded weeks yet — the ledger fills after your first gameweek.",
    },
    "model": "template-fallback",
    "cached": False,
    "generated_at": "2026-08-22T09:00:00Z",
    "facts_digest": {"captain": "Haaland", "squad_swing": 1.5, "news_matches": 0},
}


def _json_route(payload: dict | list, status: int = 200):
    def _handler(route: Route) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    return _handler


@pytest.fixture
def coherent(page: Page) -> Iterator[Page]:
    """Session pre-seeded + every endpoint a page auto-fetches mocked."""
    console_errors: list[str] = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )

    def _seed_session() -> None:
        page.evaluate(
            "(s) => localStorage.setItem('fpl_session_v20', JSON.stringify(s))", SESSION
        )

    # First visit seeds storage; subsequent gotos restore it.
    page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")
    _seed_session()

    # Safety net FIRST (later registrations win in Playwright): anything not
    # specifically mocked below returns an empty 200 payload.
    page.route("**/api/**", _json_route({}))

    page.route("**/health", _json_route({"status": "ok", "db": "ok", "version": "2.0.0"}))
    page.route("**/api/v1/decisions*", _json_route(_decisions_payload()))
    page.route("**/api/v1/squad?*", _json_route(_squad_payload()))
    page.route("**/api/v1/fixtures/scan*", _json_route(_scan_payload()))
    page.route("**/api/v1/news/radar*", _json_route({
        "session_id": SESSION["key"], "scanned_at": "2026-08-22T09:00:00Z",
        "headlines_scanned": 12, "matches_found": 1,
        "news_flags": [{
            "player_id": 3, "web_name": "Haaland",
            "headline": "Haaland returns to training after knock",
            "time": "2026-08-21T09:00:00Z", "url": "https://www.bbc.co.uk/sport/x",
        }],
    }))
    page.route(
        "**/api/v1/player/*/drawer*",
        lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(_drawer_payload(int(r.request.url.split("/player/")[1].split("/")[0]))),
        ),
    )
    page.route("**/api/v1/assistant/brief*", _json_route(BRIEF_PAYLOAD))
    page.route(
        "**/api/v1/analyst/summary*",
        _json_route({"summary": "ok", "model": "template-fallback"}),
    )
    page.route("**/api/v1/data-sources*", _json_route({"as_of": None, "sources": {}}))
    page.route("**/api/v1/players**", _json_route([
        {"id": pid, "fpl_element_id": pid, "web_name": SQUAD_PLAYERS[str(pid)]["web_name"]}
        for pid in ALL_IDS
    ]))
    page.route("**/api/v1/sync/**", _json_route({}))
    page.route("**/api/v1/league**", _json_route({"your_rank": None}))
    page.route("**/api/v1/transfers/**", _json_route({"transfers": []}))
    page.route("**/api/v1/targets**", _json_route({"targets": []}))
    page.route("**/api/v1/planner**", _json_route({"status": "no-squad"}))

    yield page

    assert console_errors == [], f"console.error lines seen: {console_errors}"


class TestSessionPersistence:
    def test_squad_renders_on_every_page_with_zero_typing(self, coherent: Page) -> None:
        page = coherent
        paths = ["/dashboard", "/my-team", "/assistant", "/track-record", "/live", "/sources"]

        for path in paths:
            page.goto(BASE_URL + path, wait_until="domcontentloaded")

            chip = page.locator('[data-testid="session-chip"]')
            expect(chip).to_have_count(1)
            expect(chip).to_contain_text("794561")
            expect(chip).to_contain_text("synced")

            if path == "/dashboard":
                pitch = page.locator("#pitchRows .player-card")
                expect(pitch.first).to_be_visible(timeout=8000)
                expect(page.locator("body")).to_contain_text("Haaland")
            elif path == "/my-team":
                tiles = page.locator('[data-testid="squad-tile"]')
                expect(tiles.first).to_be_visible(timeout=8000)
                expect(page.locator("body")).to_contain_text("Haaland")
            elif path == "/assistant":
                card = page.locator('[data-testid="brief-card"]')
                expect(card.first).to_be_visible(timeout=10000)

            # Zero typing guarantee: no input on any of these pages was touched.
            typed = page.evaluate(
                "() => document.querySelectorAll('input:not([type=hidden])')"
                ".length && Array.from(document.querySelectorAll('input'))"
                ".some(i => i.value && i.value.length > 0"
                " && i.value !== (JSON.parse(localStorage.getItem('fpl_session_v20')||'{}').key || ''))"
            )
            assert typed in (False, None), f"user had to type on {path}"

    def test_start_over_clears_session_everywhere(self, coherent: Page) -> None:
        page = coherent
        page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")
        page.click("#startOverBtn")
        expect(page.locator("#inputSection")).to_be_visible()
        stored = page.evaluate("() => localStorage.getItem('fpl_session_v20')")
        assert stored is None


class TestDrawer:
    def test_click_opens_drawer_with_form_and_fixtures(self, coherent: Page) -> None:
        page = coherent
        page.goto(BASE_URL + "/dashboard", wait_until="domcontentloaded")
        card = page.locator('.player-card[data-player-id="3"]').first
        expect(card).to_be_visible(timeout=8000)
        card.click()

        drawer = page.locator("#drawer")
        expect(drawer).to_have_class("drawer open")
        expect(drawer).to_contain_text("Haaland")

        # Phase 25 U2: the sparkline SVG replaced the bulky form bars.
        spark = page.locator('[data-testid="form-spark"] svg.form-spark')
        expect(spark).to_have_count(1)
        dots = page.locator('[data-testid="form-spark"] circle.spark-dot')
        assert dots.count() == 3, f"expected 3 spark points, got {dots.count()}"
        expect(drawer).to_contain_text("Next 5 fixtures")
        chips = drawer.locator(".fdr-chip")
        expect(chips.first).to_contain_text("WHU")
        expect(drawer).to_contain_text("xPTS breakdown")


class TestBrief:
    def test_renders_six_sections_with_real_names(self, coherent: Page) -> None:
        page = coherent
        page.goto(BASE_URL + "/assistant", wait_until="domcontentloaded")

        card = page.locator('[data-testid="brief-card"]').first
        expect(card).to_be_visible(timeout=10000)

        body = card.inner_text()
        for section in ["SQUAD STATUS", "CAPTAIN", "TRANSFERS",
                        "FIXTURE SWINGS", "NEWS FLAGS", "LAST WEEK GRADE"]:
            assert section in body, f"missing brief section {section}"
        # Real squad names appear inside the brief text.
        assert "Haaland" in body
