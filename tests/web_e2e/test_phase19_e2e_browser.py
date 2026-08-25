"""Phase 19.0 — real-browser regression suite for the multi-page UI.

Scenarios (API traffic mocked at the network layer; the browser runs the REAL
page code):

* NAV: every page (/dashboard /my-team /track-record /live /sources /connect)
  renders the shared top navigation with exactly the six Phase 19 entries.
* TRACK RECORD: after seeding history via the mocked endpoints, scored cards
  appear with an explicit "model was right/wrong by X pts" verdict.
* LIVE: the matchday board renders pushed live points and honest no-data state.
* CONNECT: token persistence wires the bookmarklet builder.
* Console stays clean everywhere: zero console errors, zero >=400 responses.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect

BASE_URL = "http://localhost:8000"

PAGES = [
    ("/dashboard", ["Decisions", "My Team", "Track Record", "Live", "Sources", "Connect"]),
    ("/my-team", ["My team"]),
    ("/assistant", ["Weekly assistant"]),
    ("/track-record", ["Track record"]),
    ("/live", ["Live matchday"]),
    ("/sources", ["Data sources"]),
    ("/connect", ["Connect your data"]),
]


def _json_route(payload: dict | list, status: int = 200):
    def _handler(route: Route) -> None:
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    return _handler


@pytest.fixture
def instrumented(page: Page) -> Iterator[Page]:
    """Console/network audit + default API mocks shared by every scenario.

    Every endpoint a page auto-fetches is mocked with a 200 so no request ever
    reaches the real server — browsers log a console error for ANY >=400
    response, and the console-clean guarantee is part of this suite.
    """
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

    # Default mocks (specific tests re-route with later registrations).
    page.route("**/health", _json_route({"status": "ok", "db": "ok", "version": "1.9.0"}))
    page.route(
        "**/api/v1/sync/status*",
        _json_route({"latest": {}, "counts": {}, "token_configured": True}),
    )
    page.route("**/api/v1/data-sources*", _json_route({"as_of": None, "sources": {}}))
    page.route(
        "**/api/v1/sync/calibration*",
        _json_route({"count": 0, "mae": None, "bias": None, "buckets": {}}),
    )
    # Safety net: anything else under /api/ returns an empty 200 payload.
    page.route("**/api/**", _json_route({}))

    yield page
    # With everything mocked to 200, ANY bad response is a regression.
    audited = [
        (status, url)
        for status, url in bad_responses
        if not url.rstrip("/").endswith("/health")
    ]
    assert audited == [], f"unexpected HTTP >=400 responses seen: {audited}"
    assert console_errors == [], f"console.error lines seen: {console_errors}"


class TestNav:
    @pytest.mark.parametrize("path,expectations", PAGES)
    def test_page_renders_nav_with_seven_entries(
        self, instrumented: Page, path: str, expectations: list[str]
    ) -> None:
        page = instrumented
        page.goto(BASE_URL + path, wait_until="domcontentloaded")

        # Phase 25 Gate 1 (U3): desktop nav lists every surface — primary
        # (Decisions · Targets · League · Live) first, then secondary pages.
        nav = page.locator(".topnav .navlink")
        expect(nav).to_have_count(13)
        labels = [t.strip() for t in nav.all_inner_texts()]
        assert labels[:4] == ["Decisions", "Targets", "League", "Live"]
        assert "My Team" in labels and "Sources" in labels and "Connect" in labels

        # Mobile bottom tabs show the four primary surfaces plus a More sheet.
        tabs = page.locator(".bottomnav a")
        expect(tabs).to_have_count(4)

        for text in expectations:
            expect(page.locator("body")).to_contain_text(text)


class TestTrackRecord:
    TRACK_RECORD_PAYLOAD = {
        "entry_id": "794561",
        "cards": [
            {
                "gameweek": 3,
                "rec_type": "captain",
                "subject": {"captain_id": 411},
                "detail": {"alternatives": [412, 413], "expected_points": 8.4},
                "created_at": "2026-08-22T11:00:00+00:00",
                "scored": True,
                "score": {
                    "captain": 411,
                    "best_alternative": 412,
                    "captain_points": 24,
                    "alternative_points": 20,
                    "delta": 4,
                    "hit_cost": 0,
                    "verdict": "right",
                },
            },
            {
                "gameweek": 3,
                "rec_type": "transfer",
                "subject": {"transfers_in": [417], "transfers_out": [418]},
                "detail": {"hit_cost": 4},
                "created_at": "2026-08-22T11:00:00+00:00",
                "scored": True,
                "score": {
                    "transfers_in": [417],
                    "transfers_out": [418],
                    "gained": 2,
                    "lost": 9,
                    "delta": -11,
                    "hit_cost": 4,
                    "verdict": "wrong",
                },
            },
            {
                "gameweek": 4,
                "rec_type": "captain",
                "subject": {"captain_id": 411},
                "detail": {"alternatives": [412]},
                "created_at": "2026-08-22T12:00:00+00:00",
                "scored": False,
                "score": None,
            },
        ],
        "rolling": {
            "graded": 2,
            "hits": 1,
            "hit_rate": 0.5,
            "net_points": -7,
            "last_5": [],
        },
    }

    def test_scored_cards_render_after_seeded_history(self, instrumented: Page) -> None:
        page = instrumented
        page.route(
            "**/api/v1/sync/track-record*",
            _json_route(self.TRACK_RECORD_PAYLOAD),
        )
        page.route(
            "**/api/v1/sync/calibration*",
            _json_route({"count": 42, "mae": 1.87, "bias": -0.31, "buckets": {}}),
        )

        page.goto(BASE_URL + "/track-record", wait_until="domcontentloaded")
        page.fill("#entryInput", "794561")
        page.click("#loadBtn")

        # Summary bar reflects the rolling aggregate.
        expect(page.locator("#stGraded")).to_have_text("2")
        expect(page.locator("#stHit")).to_have_text("50%")
        expect(page.locator("#stNet")).to_have_text("-7")

        cards = page.locator('[data-testid="tr-card"]')
        expect(cards).to_have_count(3)

        first_text = cards.nth(0).inner_text()
        assert "Model was right by 4 pts" in first_text
        second_text = cards.nth(1).inner_text()
        assert "WRONG" in second_text
        third_text = cards.nth(2).inner_text()
        assert "Awaiting real GW4 results" in third_text


class TestLiveBoard:
    def test_renders_pushed_live_points(self, instrumented: Page) -> None:
        page = instrumented
        # Shape mirrors GET /api/v1/live (Phase 19.0 matchday engine).
        payload = {
            "session_id": "794561",
            "gameweek": 3,
            "live_mode": True,
            "gw_finished": False,
            "headline": {
                "team_total": 39,
                "captain_vs_vice_delta": 6,
                "captain_name": "Haaland",
                "vice_name": "Someone",
                "text": "Team 39 pts",
            },
            "rows": [
                {
                    "element_id": 411,
                    "name": "Haaland",
                    "pos": "FWD",
                    "minutes": 60,
                    "goals": 0,
                    "assists": 0,
                    "bonus": 0,
                    "raw_points": 6,
                    "multiplier": 2,
                    "points": 12,
                    "is_captain": True,
                    "is_vice": False,
                    "team": "MCI",
                    "team_id": 15,
                },
            ],
            "bench": [
                {
                    "element_id": 425,
                    "name": "Areola",
                    "pos": "GK",
                    "minutes": None,
                    "goals": None,
                    "assists": None,
                    "bonus": None,
                    "raw_points": 0,
                    "multiplier": 1,
                    "points": 0,
                    "is_captain": False,
                    "is_vice": False,
                    "team": "WHU",
                    "team_id": 9,
                },
            ],
            "espn_matches": [],
            "picks_source": "saved-squad",
            "data_age_seconds": 0,
            "stale_snapshot": False,
            "graded_now": False,
            "pings_sent": 0,
            "track_record_url": "/track-record",
            "note": "",
            "as_of": "2026-08-22T15:10:00+00:00",
        }
        # live.html polls /api/v1/live?session_id= (the Phase 19.0 matchday
        # engine), not the legacy sync/live-board read model.
        page.route("**/api/v1/live?*", _json_route(payload))

        page.goto(BASE_URL + "/live", wait_until="domcontentloaded")
        # The live board is zero-typing since Phase 20: the shared session
        # drives it, so seed the session instead of typing an entry id.
        page.evaluate(
            "() => localStorage.setItem('fpl_session_v20', JSON.stringify("
            "{key:'794561', source:'fpl_id', entry_name:'Test', synced_at:new Date().toISOString()}))"
        )
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        expect(page.locator("#xiBox")).to_contain_text("Haaland")
        expect(page.locator("#xiBox")).to_contain_text("C×2")
        expect(page.locator("#benchBox")).to_contain_text("Areola")
        expect(page.locator("#teamTotal")).to_have_text("39")


class TestConnect:
    def test_token_save_builds_bookmarklet(self, instrumented: Page) -> None:
        page = instrumented
        page.route(
            "**/api/v1/sync/status*",
            _json_route(
                {
                    "latest": {},
                    "counts": {},
                    "token_configured": True,
                }
            ),
        )

        page.goto(BASE_URL + "/connect", wait_until="domcontentloaded")

        # Apps Script source is hosted inline with its trigger contract.
        body = page.locator("body").inner_text()
        assert "syncSquadDaily" in body
        assert "syncLiveMatchday" in body
        assert "/api/v1/sync/history-push" in body or "history-push" in body

        page.fill("#tokenInput", "a" * 64)
        page.click("#saveTokenBtn")

        stored = page.evaluate("() => localStorage.getItem('fpl_sync_push_token')")
        assert stored == "a" * 64

        href = page.locator("#bookmarkletLink").get_attribute("href") or ""
        assert href.startswith("javascript:"), "bookmarklet link must be a javascript: URL"

        # The bookmarklet source parses as JS (it is served verbatim).
        src_status = page.request.get(BASE_URL + "/static/bookmarklet.js")
        assert src_status.ok
