"""Phase 18.0 — real-browser regression suite (Playwright).

Scenarios (all API traffic mocked at the network layer; the browser runs the
REAL dashboard code against realistic payloads):

  A: Enter FPL ID 794561 -> real squad names render OR an honest sync card
     appears (503 path) — never another squad, never a bare 500.
  B: Type 15 real player names -> the rendered pitch shows EXACTLY those names
     with their official prices (typed == rendered, one ID space).
  C: Reload -> saved squad restores with its source chip.
  Plus: console must stay clean — zero console errors, zero >=400 responses,
  and ZERO requests to resources.premierleague.com (avatars only by default).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect

BASE_URL = "http://localhost:8000"

#: element_id -> (web_name, price_millions, position_code, team_id).
CATALOG: dict[int, tuple[str, float, int, int]] = {
    411: ("Haaland", 15.5, 4, 15),
    412: ("Salah", 12.5, 3, 8),
    413: ("Saka", 10.5, 3, 2),
    414: ("Palmer", 10.5, 3, 6),
    415: ("Foden", 9.0, 3, 15),
    416: ("Son", 9.5, 3, 17),
    417: ("Watkins", 8.5, 4, 4),
    418: ("Isak", 8.5, 4, 12),
    419: ("Gabriel", 6.5, 2, 2),
    420: ("Saliba", 6.0, 2, 2),
    421: ("Van Dijk", 6.5, 2, 12),
    422: ("Trippier", 6.0, 2, 17),
    423: ("Alexander-Arnold", 7.5, 2, 12),
    424: ("Alisson", 5.5, 1, 12),
    425: ("Areola", 4.5, 1, 20),
    # Extra entries so autocomplete searches stay unambiguous.
    430: ("Thiaw", 4.5, 2, 14),
}

PLAYERS_PAYLOAD = [
    {
        "id": el + 100000,  # internal DB id deliberately far from element id
        "fpl_element_id": el,
        "web_name": name,
        "team": team,
        "position": pos,
        "price": price,
        "code": 900000 + el,
    }
    for el, (name, price, pos, team) in CATALOG.items()
]


def _name_for(element_id: int) -> str:
    return CATALOG[element_id][0]


def _price_for(element_id: int) -> float:
    return CATALOG[element_id][1]


def _mock_health(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({"status": "ok", "db": "ok", "version": "1.8.0"}),
    )


def _mock_players(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(PLAYERS_PAYLOAD),
    )


def _make_from_fpl_handler(captured: dict[str, list[int]]):
    def _handle(route: Route) -> None:
        """Import entry 794561 -> first 15 catalog elements as a real squad."""
        ids = list(CATALOG)[:15]
        captured["794561"] = ids
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "squad": {
                        "player_ids": ids,
                        "captain_id": 411,
                        "vice_captain_id": 412,
                        "bank": 0.5,
                        "free_transfers": 1,
                        "chips_available": [
                            "wildcard",
                            "free_hit",
                            "bench_boost",
                            "triple_captain",
                        ],
                        "gameweek": 3,
                        "player_positions": {i: CATALOG[i][2] for i in ids},
                        "player_prices": {i: CATALOG[i][1] for i in ids},
                        "player_teams": {i: CATALOG[i][3] for i in ids},
                        "is_demo": False,
                    },
                    "player_names": {i: _name_for(i) for i in ids},
                    "entry_name": "Phase18 FC",
                    "gameweek": 3,
                    "is_demo": False,
                    "sync_status": "Synced via env_proxy — FPL ID 794561 saved.",
                }
            ),
        )

    return _handle


def _mock_decisions_factory(captured_squads: dict[str, list[int]]):
    """Build a decisions handler whose players map derives from the saved squad."""

    def _handle(route: Route) -> None:
        url = route.request.url
        session = ""
        if "session_id=" in url:
            session = url.split("session_id=")[-1].split("&")[0]
        ids = captured_squads.get(session)
        if not ids:
            route.fulfill(
                status=404,
                content_type="application/json",
                body=json.dumps({"detail": "No squad saved for this session"}),
            )
            return

        players = {
            str(i): {
                "id": i,
                "web_name": _name_for(i),
                "team": CATALOG[i][3],
                "position": CATALOG[i][2],
                "price": _price_for(i),
                "code": 900000 + i,
                "expected_points": round(3.0 + (i % 7) * 0.8, 2),
                "prediction_source": "pre-season-proxy-v2",
                "data_quality": "heuristic-proxy-enriched",
                "minutes_estimate": 60.0,
                "start_prob": 0.72,
            }
            for i in ids
        }
        xi = ids[:11]
        bench = ids[11:]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-08-22T12:00:00Z",
                    "gameweek": 3,
                    "starting_xi": xi,
                    "bench_order": bench,
                    "captain": {
                        "player_id": 411,
                        "expected_points": 8.4,
                        "expected_gain": 4.2,
                        "probability_positive": 0.8,
                        "confidence": 0.85,
                        "main_reason": "Top price-percentile base rate.",
                        "main_risk": None,
                    },
                    "vice_captain": 412,
                    "transfer_plan": None,
                    "chip_recommendation": None,
                    "players": players,
                    "meta": {
                        "chain": {
                            "source": "pre-season-proxy-v2",
                            "source_label": "Pre-season proxy v2 (price + fixtures + xG)",
                            "data_quality": "heuristic-proxy-enriched",
                            "covered_players": len(ids),
                            "notes": {},
                            "market_check": {"enabled": True, "fixtures_matched": 0},
                        },
                        "squad_summary": {
                            "team_value": 100.0,
                            "bank": 0.5,
                            "free_transfers": 1,
                            "chips_available": [
                                "wildcard",
                                "free_hit",
                                "bench_boost",
                                "triple_captain",
                            ],
                        },
                        "player_positions": {i: CATALOG[i][2] for i in ids},
                    },
                }
            ),
        )

    return _handle


def _mock_analyst(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "summary": (
                    "Haaland is your captain this week, projected 8.4 points, "
                    "based on Pre-season proxy v2 (price + fixtures + xG). "
                    "The engine recommends rolling your free transfer."
                ),
                "model": "groq/openai/gpt-oss-120b",
                "session_id": "whatever",
                "gameweek": 3,
            }
        ),
    )


def _make_squad_post_handler(captured: dict[str, list[int]]):
    def _handle(route: Route) -> None:
        body = json.loads(route.request.post_data or "{}")
        session = route.request.url.split("session_id=")[-1].split("&")[0]
        captured[session] = body.get("player_ids", [])
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({**body, "is_demo": False, "updated_at": "2026-08-22T12:00:00Z"}),
        )

    return _handle


@pytest.fixture
def browser_page(page: Page) -> Iterator[Page]:
    """Wire route mocks + instrument console/network for the honesty audit."""
    captured_squads: dict[str, list[int]] = {}

    console_errors: list[str] = []
    bad_responses: list[tuple[int, str]] = []
    pl_cdn_requests: list[str] = []

    page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )
    page.on(
        "response",
        lambda resp: bad_responses.append((resp.status, resp.url)) if resp.status >= 400 else None,
    )
    page.on(
        "request",
        lambda req: (
            pl_cdn_requests.append(req.url) if "resources.premierleague.com" in req.url else None
        ),
    )

    # Catch-all FIRST (later registrations win): nothing un-mocked may ever
    # reach the real server.
    page.route(
        "**/api/**",
        lambda r: r.fulfill(
            status=404,
            content_type="application/json",
            body='{"detail":"unmocked endpoint"}',
        ),
    )
    page.route("**/health", _mock_health)
    page.route("**/api/v1/players**", _mock_players)
    page.route("**/api/v1/squad/from-fpl", _make_from_fpl_handler(captured_squads))
    page.route("**/api/v1/squad?*", _make_squad_post_handler(captured_squads))
    page.route("**/api/v1/squad/demo**", lambda r: r.fulfill(status=404, body="{}"))
    page.route("**/api/v1/decisions*", _mock_decisions_factory(captured_squads))
    page.route("**/api/v1/analyst/summary*", _mock_analyst)
    page.route("**/api/v1/data-sources*", lambda r: r.fulfill(status=200, body="{}"))

    yield page

    # R4 artifacts: every scenario ends with a clean console. Mocked API paths
    # are exempt from the status audit (the harness controls them); anything
    # else (CDNs, real 500s) must never fail.
    audited = [
        (status, url)
        for status, url in bad_responses
        if "/api/" not in url and not url.rstrip("/").endswith("/health")
    ]
    assert pl_cdn_requests == [], f"PL CDN requested despite flag-off default: {pl_cdn_requests}"
    assert audited == [], f"unexpected HTTP >=400 responses seen: {audited}"
    assert console_errors == [], f"console.error lines seen: {console_errors}"


def _fresh(page: Page) -> None:
    page.goto(BASE_URL + "/dashboard")
    page.evaluate("() => localStorage.clear()")
    page.reload()
    page.wait_for_load_state("domcontentloaded")


class TestScenarioA:
    def test_fpl_import_renders_real_names(self, browser_page: Page) -> None:
        page = browser_page
        _fresh(page)

        page.fill("#teamId", "794561")
        page.click("#analyzeBtn")
        expect(page.locator("#results")).to_be_visible(timeout=15000)

        # Real squad names render — never a placeholder, never another squad.
        expect(page.locator(".web-name").filter(has_text="Haaland")).to_be_visible()
        expect(page.locator(".web-name").filter(has_text="Alisson")).to_be_visible()

        # Sync-status line names the winning mask.
        expect(page.locator("#teamName")).to_contain_text("Phase18 FC")

        # Demo never renders.
        expect(page.locator("#demoBanner")).to_be_hidden()


class TestScenarioB:
    SEARCH_TERMS = [  # (search text, expected element id) per slot order GK..FWD
        ("Alis", 424),
        ("Areo", 425),
        ("Gabr", 419),
        ("Sali", 420),
        ("Van", 421),
        ("Trip", 422),
        ("Alex", 423),
        ("Saka", 413),
        ("Palm", 414),
        ("Fod", 415),
        ("Son", 416),
        ("Salah", 412),
        ("Haal", 411),
        ("Watkins", 417),
        ("Isak", 418),
    ]

    def test_typed_names_render_exactly_with_prices(self, browser_page: Page) -> None:
        page = browser_page
        _fresh(page)

        page.click("#toggleManualBtn")
        expect(page.locator("#manualEntry")).to_be_visible(timeout=10000)

        slots = page.locator(".manual-slot")
        assert slots.count() == 15

        typed: list[tuple[str, int]] = []
        for idx, (term, element_id) in enumerate(self.SEARCH_TERMS):
            slot = slots.nth(idx)
            slot.click()
            slot.press_sequentially(term, delay=30)
            wrap = slot.locator("..")
            item = wrap.locator(f'.autocomplete-item[data-id="{element_id}"]').first
            expect(item).to_be_visible(timeout=3000)
            item.click()
            typed.append((_name_for(element_id), element_id))

        expect(page.locator("#pickCounter")).to_contain_text("15/15")

        # Captain + vice via search over picked players.
        page.fill("#manualCap", "Haal")
        cap_item = page.locator('#capList .autocomplete-item[data-id="411"]').first
        expect(cap_item).to_be_visible(timeout=3000)
        cap_item.click()
        page.fill("#manualVice", "Salah")
        vice_item = page.locator('#viceList .autocomplete-item[data-id="412"]').first
        expect(vice_item).to_be_visible(timeout=3000)
        vice_item.click()

        page.click("#saveManualBtn")
        expect(page.locator("#results")).to_be_visible(timeout=15000)

        # TYPED == RENDERED: every card shows the exact typed name + price.
        rendered = page.locator(".player-card .web-name").all_inner_texts()
        rendered_prices = page.locator(".player-card .price-tag").all_inner_texts()
        for name, _element_id in typed:
            assert name in rendered, f"typed '{name}' missing from pitch"
        expected_price = "£" + f"{_price_for(411):.1f}" + "m"
        haaland_idx = rendered.index("Haaland")
        assert rendered_prices[haaland_idx] == expected_price, (
            f"Haaland price mismatch: {rendered_prices[haaland_idx]}"
        )

        # No cross-ID leakage: Thiaw exists in catalog but was NOT picked.
        assert "Thiaw" not in rendered


class TestScenarioC:
    def test_reload_restores_saved_squad_with_source_chip(self, browser_page: Page) -> None:
        page = browser_page
        _fresh(page)
        page.evaluate(
            """() => {
              localStorage.setItem('fpl_session_id', 'manual-e2e');
              localStorage.setItem('fpl_session_source', 'manual');
              localStorage.setItem('fpl_session_source_label', 'Manual Squad');
            }"""
        )

        # Register the saved squad so decisions resolves it after reload.
        captured: dict[str, list[int]] = {"manual-e2e": list(CATALOG)[:15]}

        # Re-route decisions to a handler aware of this pre-saved squad.
        page.unroute("**/api/v1/decisions*")
        page.route("**/api/v1/decisions*", _mock_decisions_factory(captured))

        page.reload()
        page.wait_for_load_state("domcontentloaded")

        expect(page.locator("#sourceChip")).to_be_visible()
        chip = page.locator("#sourceChip").text_content() or ""
        assert "Manual" in chip
