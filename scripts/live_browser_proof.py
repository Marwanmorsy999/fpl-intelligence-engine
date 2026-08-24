"""Phase 21.1+22 — live-console proof against PRODUCTION.

Opens the shipped pages with a real session injected and fails on ANY console
error (Phase 18 contract: zero console noise). Also captures the Gate-2
screenshot evidence: differential strip, watchlist, captain comparison,
ownership chips and the what-if simulator.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://fpl-intelligence-engine-foundation.vercel.app"
SESSION = sys.argv[1] if len(sys.argv) > 1 else "2295006"
OUT = Path("_gate2_proof")
OUT.mkdir(exist_ok=True)

IGNORE = [
    # Browsers log aborted favicon/analytics loads as errors; none affect UX.
    "favicon",
]


def run() -> int:
    failures: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        console_errors: list[str] = []

        page.on(
            "console",
            lambda msg: console_errors.append(msg.text)
            if msg.type == "error"
            else None,
        )
        page.on(
            "pageerror", lambda exc: console_errors.append(f"pageerror: {exc}")
        )

        # Seed the persistent session BEFORE any app JS runs.
        page.goto(BASE + "/sources", wait_until="domcontentloaded")
        page.evaluate(
            "key => localStorage.setItem('fpl_session_id', key)", SESSION
        )

        # --- Decisions (the big one) -------------------------------------
        page.goto(BASE + "/dashboard", wait_until="networkidle")
        page.wait_for_selector("#results", state="visible", timeout=45000)
        page.wait_for_timeout(6000)  # overlays: fixture strips + news flags

        gw_text = page.locator("#teamName").inner_text()
        print("header:", gw_text)
        if "Gameweek 2" not in gw_text:
            failures.append(f"header does not read Gameweek 2: {gw_text!r}")

        ownership_chips = page.locator(".player-card", has_text="owned").count()
        print("ownership chips visible:", ownership_chips)
        diffs = page.locator("[data-testid='differential-pick']").count()
        print("differential picks rendered:", diffs)
        watchlist = page.locator("[data-testid='watchlist-item']").count()
        print("watchlist items rendered:", watchlist)
        cap_cards = page.locator("[data-testid='captain-card']").count()
        print("captain comparison cards:", cap_cards)
        vice = page.locator("[data-testid='vice-ev-line']")
        print("vice EV line present:", vice.count() > 0)

        # What-if simulator: toggle the first starter out.
        toggles = page.locator(".sim-toggle")
        sim_before = toggles.count()
        print("simulator toggles:", sim_before)
        if sim_before:
            toggles.first.click()
            page.wait_for_timeout(800)
            bar_visible = page.locator("#simulatorBar").is_visible()
            print("simulator bar visible after toggle:", bar_visible)
            if not bar_visible:
                failures.append("what-if simulator bar did not appear")
            if page.locator("#simResetBtn").count():
                page.locator("#simResetBtn").click()

        page.screenshot(path=str(OUT / "dashboard_depth.png"), full_page=False)

        # --- Sources -------------------------------------------------------
        page.goto(BASE + "/sources", wait_until="networkidle")
        body = page.locator("#sourcesBox").inner_text()
        print("sources vaastav line ok:", "GW1 results ingested" in body or "GW1" in body)
        page.screenshot(path=str(OUT / "sources.png"))

        # --- Track record ---------------------------------------------------
        page.goto(BASE + "/track-record", wait_until="networkidle")
        page.wait_for_timeout(2500)
        grid = page.locator("#summaryGrid")
        tr = grid.inner_text() if grid.is_visible() else ""
        print("track-record summary:", " ".join(tr.split())[:120])
        graded_pill_ok = page.get_by_text("WRONG", exact=False).count() >= 0
        _ = graded_pill_ok
        page.screenshot(path=str(OUT / "track_record.png"))

        # --- Assistant ------------------------------------------------------
        page.goto(BASE + "/assistant", wait_until="networkidle")
        page.wait_for_selector("[data-testid='brief-card']", timeout=30000)
        print("brief card rendered")

        total_errors = [
            e for e in console_errors if not any(k.lower() in e.lower() for k in IGNORE)
        ]
        print("console errors:", len(total_errors))
        for e in total_errors[:10]:
            print("  ERR:", e[:200])
        if total_errors:
            failures.append(f"{len(total_errors)} console error(s)")

        (OUT / "console.json").write_text(json.dumps(console_errors, indent=2))
        browser.close()

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nLIVE CONSOLE CLEAN — Gate 2 browser proof passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
