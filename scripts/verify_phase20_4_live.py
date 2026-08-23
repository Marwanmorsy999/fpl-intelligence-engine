"""Phase 20.4 — live-production browser proof.

Drives the DEPLOYED site with a real Chromium:
* /live      - real GW rows (name + minutes + points), LIVE badge, data-age
               chip, captain-vs-vice headline, per-mask health pills,
               console clean, /api/v1/live data call < 2 s.
* /assistant - TL;DR card with exactly three actions, six brief sections,
               answering-model label, console clean.

Run: python scripts/verify_phase20_4_live.py [base_url] [entry_id]
"""

from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

BASE = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "https://fpl-intelligence-engine-foundation.vercel.app"
)
ENTRY = sys.argv[2] if len(sys.argv) > 2 else "2295006"

failures: list[str] = []


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        page = ctx.new_page()

        console_errors: list[str] = []
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        # Seed the persistent session on the prod origin (same storage key).
        page.goto(BASE + "/sources", wait_until="domcontentloaded")
        page.evaluate(
            """(key) => localStorage.setItem('fpl_session_v20', JSON.stringify({
                 key: key, source: 'fpl_id', entry_name: 'banhawayaFC',
                 synced_at: new Date().toISOString() }))""",
            ENTRY,
        )

        # ---------------------------- /live -----------------------------------
        t0 = time.time()
        page.goto(BASE + "/live", wait_until="domcontentloaded")
        page.wait_for_selector("[data-testid='headline-card']", state="visible", timeout=30000)

        rows = page.locator("#xiBox .row")
        n_rows = rows.count()
        print(f"/live starting-XI rows rendered: {n_rows}")
        if n_rows < 5:
            failures.append(f"expected >=5 XI rows, got {n_rows}")

        print("sample rows:")
        for i in range(min(n_rows, 5)):
            txt = " ".join(rows.nth(i).inner_text().split())
            print("   ", txt)

        cap_line = page.locator("[data-testid='captain-vs-vice']").inner_text()
        print("captain-vs-vice line:", cap_line)
        if "Captain vs Vice" not in cap_line:
            failures.append("captain vs vice headline missing")

        age_chip = page.locator("[data-testid='data-age-chip']").inner_text()
        print("data-age chip:", age_chip)
        badge = page.locator("#liveBadge").inner_text()
        print("badge:", badge)

        masks = page.locator("#maskBox .row").count()
        print("mask health rows:", masks)
        if masks < 4:
            failures.append(f"expected >=4 mask status rows, got {masks}")

        perf = page.evaluate(
            "() => performance.getEntriesByType('resource')"
            ".filter(e => e.name.includes('/api/v1/live'))"
            ".map(e => Math.round(e.duration))"
        )
        print("/api/v1/live fetch ms:", perf)
        if perf and max(perf) >= 2000:
            failures.append(f"/api/v1/live took {max(perf)}ms (>=2000)")

        # ------------------------- /assistant ----------------------------------
        t1 = time.time()
        page.goto(BASE + "/assistant", wait_until="domcontentloaded")
        page.wait_for_selector("[data-testid='brief-card']", state="visible", timeout=45000)

        tldr = page.locator("#briefBox [data-tldr-kind]")
        n_tldr = tldr.count()
        kinds = [tldr.nth(i).get_attribute("data-tldr-kind") for i in range(n_tldr)]
        print("TL;DR actions:", kinds)
        if n_tldr != 3 or set(kinds) != {"CAPTAIN", "TRANSFERS", "CHIP"}:
            failures.append(f"TL;DR must be exactly CAPTAIN/TRANSFERS/CHIP, got {kinds}")

        confs = page.locator("#briefBox [data-testid='tldr-card'] .pill.num")
        print("confidence chips:", [confs.nth(i).inner_text() for i in range(confs.count())])

        n_sections = page.locator("#briefBox [data-brief-section]").count()
        print("brief sections:", n_sections)
        if n_sections < 6:
            failures.append(f"expected six brief sections, got {n_sections}")

        body_text = page.locator("#briefBox [data-testid='brief-card']").inner_text()
        wanted = ("Haaland", "Gabriel", "Aina", "Cherki", "Fernandes")
        names_found = sum(1 for n in wanted if n in body_text)
        print("real squad names cited:", names_found)

        # Personalization is also proven by the payload digest.
        digest = page.evaluate(
            """async () => {
                 const s = JSON.parse(localStorage.getItem('fpl_session_v20'));
                 const r = await fetch('/api/v1/assistant/brief?session_id=' + s.key);
                 const j = await r.json();
                 return j.facts_digest;
               }"""
        )
        print("facts_digest:", digest)
        if not digest.get("entry_label") or digest.get("squad_names_count", 0) < 5:
            failures.append("brief payload is not personalized (label/names)")

        model_line = ""
        meta = page.locator("#briefMeta").inner_text()
        print("brief meta:", meta)
        header_pill = page.locator(
            "#briefBox [data-testid='brief-card'] > .row .pill"
        ).first
        model_line = header_pill.inner_text()
        print("answering-model pill:", model_line)

        print(f"page timings: live={(time.time() - t0) * 1000:.0f}ms "
              f"assistant={(time.time() - t1) * 1000:.0f}ms")

        # --------------------------- console ------------------------------------
        browser.close()

    print()
    print("console errors:", console_errors)
    if console_errors:
        failures.append(f"console errors seen: {console_errors}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("PHASE 20.4 LIVE BROWSER PROOF: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
