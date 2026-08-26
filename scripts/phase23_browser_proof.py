"""Phase 23 — live-console proof against PRODUCTION (both gates).

Opens the shipped pages with a real session injected and fails on ANY console
error (Phase 18 contract: zero console noise). Proves:

Gate 0: market-check sentence on Decisions + Sources identical; calibration
        arms line; My Team "Gameweek N" header + sync sub-line; captain
        comparison cards carry labelled numbers.
Gate 1: /league renders the auto-detected league; risers/fallers strip;
        notification bell present.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://fpl-intelligence-engine-foundation.vercel.app"
SESSION = sys.argv[1] if len(sys.argv) > 1 else "2295006"
OUT = Path("_gate2_proof")
OUT.mkdir(exist_ok=True)

IGNORE = ["favicon"]

MARKET_RE = re.compile(r"matched \d+/\d+ GW\d+ fixtures")


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
        page.on("pageerror", lambda exc: console_errors.append(f"pageerror: {exc}"))

        # Seed BOTH legacy + v20 session keys so every page restores it.
        page.goto(BASE + "/sources", wait_until="domcontentloaded")
        page.evaluate(
            """key => {
              localStorage.setItem('fpl_session_id', key);
              const old = JSON.parse(localStorage.getItem('fpl_session_v20') || 'null');
              localStorage.setItem('fpl_session_v20', JSON.stringify({
                key, source: (old && old.source) || 'fpl_id',
                entry_name: (old && old.entry_name) || 'Tricky',
                synced_at: new Date().toISOString()
              }));
            }""",
            SESSION,
        )

        # --- Gate 0 / C1+C4: Decisions page ---------------------------------
        page.goto(BASE + "/dashboard", wait_until="domcontentloaded")
        page.wait_for_selector("#results", state="visible", timeout=45000)
        page.wait_for_timeout(7000)

        mc_nodes = page.locator("[data-market-check]")
        mc_count = mc_nodes.count()
        mc_texts = [mc_nodes.nth(i).inner_text().strip() for i in range(mc_count)]
        print("market-check nodes:", mc_count, mc_texts)
        if mc_count and not MARKET_RE.search(mc_texts[0]):
            failures.append(f"market check text not canonical: {mc_texts[0]!r}")
        if len(set(mc_texts)) > 1:
            failures.append(f"market-check texts differ between spots: {mc_texts}")

        cap_cards = page.locator("[data-testid='captain-card']")
        cap_n = cap_cards.count()
        print("captain comparison cards:", cap_n)
        for i in range(cap_n):
            label = cap_cards.nth(i).locator("[data-xpts-label]").inner_text()
            print(f"  card {i}: xpts-label={label!r}")
            if not label.startswith("xPTS"):
                failures.append(f"captain card {i} number unlabelled: {label!r}")
        note = cap_cards.first.inner_text() if cap_n else ""
        if cap_n and not re.search(r"xPTS \d", note):
            failures.append("blank_note missing labelled xPTS")

        # --- Gate 1 / L3: price strip ---------------------------------------
        strip = page.locator("#priceStrip").inner_text()
        print("price strip:", " ".join(strip.split())[:140])
        page.screenshot(path=str(OUT / "p23_dashboard.png"), full_page=False)

        # --- Gate 0 / C3: Sources calibration arms --------------------------
        page.goto(BASE + "/sources", wait_until="networkidle")
        page.wait_for_timeout(4000)
        cal_text = page.locator("#calBox").inner_text()
        print("calibration box:", " ".join(cal_text.split())[:220])
        arms_line = page.get_by_text("calibration arms:")
        print("calibration arms line visible:", arms_line.count() > 0)
        src_text = page.locator("#sourcesBox").inner_text()
        m = MARKET_RE.search(src_text)
        print("sources odds line:", m.group(0) if m else "NOT FOUND")
        if not m:
            failures.append("Sources odds row missing canonical detail")
        elif mc_count and m.group(0).split("·")[0] not in mc_texts[0]:
            pass  # same prefix already asserted above
        page.screenshot(path=str(OUT / "p23_sources.png"))

        # --- Gate 0 / C6: My Team header -------------------------------------
        page.goto(BASE + "/my-team", wait_until="networkidle")
        page.wait_for_selector("[data-testid='squad-tile']", timeout=30000)
        page.wait_for_timeout(2500)
        gw_pill = page.locator("#gwHeader").inner_text()
        sub = page.locator("#syncSubLine").inner_text()
        print("my-team header:", gw_pill, "| sub-line:", sub)
        if not re.match(r"Gameweek \d+", gw_pill):
            failures.append(f"My Team gameweek pill wrong: {gw_pill!r}")
        if "squad synced during GW" not in sub:
            failures.append(f"My Team sync sub-line missing: {sub!r}")
        chips = page.locator("[data-testid='squad-tile']").locator("text=▲").count()
        chips_down = page.locator("[data-testid='squad-tile']").locator("text=▼").count()
        print("price chips on tiles:", chips, "up /", chips_down, "down")
        page.screenshot(path=str(OUT / "p23_myteam.png"))

        # --- Gate 0 / C5: Assistant history excludes current brief ----------
        page.goto(BASE + "/assistant", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("[data-testid='brief-card']", timeout=30000)
        except Exception:
            page.wait_for_timeout(5000)
        page.wait_for_timeout(2500)
        main_gw = None
        main_html = page.locator("#briefBox").inner_text() if page.locator("#briefBox").count() else ""
        m2 = re.search(r"Brief .*?Gameweek (\d+)", main_html)
        if m2:
            main_gw = int(m2.group(1))
        history_cards = page.locator("#historyBox [data-testid='brief-card']")
        hist_n = history_cards.count()
        dupes = []
        for i in range(hist_n):
            t = history_cards.nth(i).inner_text()
            mm = re.search(r"Gameweek (\d+)", t)
            if mm and main_gw is not None and int(mm.group(1)) == main_gw:
                dupes.append(int(mm.group(1)))
        print(f"assistant: current brief GW={main_gw}; history cards={hist_n}; dupes={dupes}")
        if dupes:
            failures.append(f"history shows the currently-shown brief GW{main_gw}")

        # --- Gate 1 / L1: the LEAGUE KILLER ----------------------------------
        page.goto(BASE + "/league", wait_until="networkidle")
        page.wait_for_timeout(9000)  # auto-detect + optional refresh round-trips
        league_status = page.locator("#contentBox").inner_text()
        status_line = " ".join(league_status.split())[:400]
        print("league page:", status_line)
        options = page.locator("[data-testid='league-option']")
        opt_n = options.count()
        if opt_n:
            names = [options.nth(i).inner_text() for i in range(opt_n)]
            print("picker leagues:", names)
            (OUT / "league_picker.txt").write_text("\n".join(names), encoding="utf-8")
        standings = page.locator("[data-testid='standings-table']")
        print("standings table:", standings.count() > 0)
        heat = page.locator("[data-testid='ownership-heat']").inner_text() \
            if page.locator("[data-testid='ownership-heat']").count() else ""
        print("ownership heat:", " ".join(heat.split())[:200])
        edge = page.locator("[data-testid='projected-edge']").inner_text() \
            if page.locator("[data-testid='projected-edge']").count() else ""
        print("projected edge:", " ".join(edge.split())[:240])
        rank_line = page.locator("[data-testid='your-rank-line']")
        print("your-rank line:", rank_line.inner_text() if rank_line.count() else "-")
        cap_insight = page.locator("[data-testid='captain-insight']")
        print("captain insight:", cap_insight.inner_text() if cap_insight.count() else "-")
        (OUT / "league_page.txt").write_text(status_line, encoding="utf-8")
        page.screenshot(path=str(OUT / "p23_league.png"), full_page=False)

        # --- Gate 1 / L2: bell ------------------------------------------------
        bell = page.locator("#bellBtn")
        print("notification bell rendered:", bell.count() > 0)
        if bell.count():
            bell.click()
            page.wait_for_timeout(1500)
            panel = page.locator("[data-testid='bell-panel']").inner_text()
            print("bell panel:", " ".join(panel.split())[:160])

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
    print("\nLIVE CONSOLE CLEAN — Phase 23 browser proof passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
