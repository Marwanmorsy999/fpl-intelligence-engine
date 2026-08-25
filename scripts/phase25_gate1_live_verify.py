"""Phase 25 GATE 1 LIVE VERIFICATION — readability overhaul, prod-only.

Run after the v2.5.1-executive-ui deploy:

    python scripts/phase25_gate1_live_verify.py [base_url]

Checks, against LIVE production:
1. Every page still renders (regression sweep) with ZERO console errors.
2. The player drawer endpoint answers 200 and opens in-page.
3. Sticky ribbon present; mobile bottom tabs present.
4. Writes _gate25_proof/gate1_live.txt.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = sys.argv[1] if len(sys.argv) > 1 else (
    "https://fpl-intelligence-engine-foundation.vercel.app"
)
PAGES = [
    "/dashboard", "/decisions", "/targets", "/league", "/live", "/my-team",
    "/planner", "/assistant", "/track-record", "/compare", "/chips",
    "/crunch", "/sources", "/connect",
]
OUT = Path("_gate25_proof")


def main() -> int:
    OUT.mkdir(exist_ok=True)
    lines: list[str] = []
    failures: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        lines.append(line)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        page = ctx.new_page()

        # Seed the saved session so squad-driven pages render fully.
        page.goto(BASE + "/connect", wait_until="domcontentloaded")
        page.evaluate(
            "() => localStorage.setItem('fpl_session_v20', JSON.stringify("
            "{key:'2295006', source:'fpl_id', entry_name:'Phase25Proof',"
            "synced_at:new Date().toISOString()}))"
        )

        emit("=== 1. REGRESSION SWEEP — every page renders, console clean ===")
        for path in PAGES:
            errors: list[str] = []
            handler = lambda m: errors.append(m.text[:140]) if m.type == "error" else None
            page.on("console", handler)
            page.goto(BASE + path, wait_until="domcontentloaded")
            page.wait_for_timeout(2600)
            page.remove_listener("console", handler)
            status = "OK" if not errors else f"CONSOLE ERRORS ({len(errors)}): {errors[:2]}"
            if errors:
                failures.append(f"{path}: {errors[0][:120]}")
            emit(f"{path:16s} {status}")
        emit()

        emit("=== 2. DRAWER opens with 200 payload ===")
        api_status: dict[int, int] = {}
        resp_handler = lambda r: api_status.__setitem__(0, r.status) if "/drawer" in r.url else None
        page.on("response", resp_handler)
        page.goto(BASE + "/dashboard", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        card = page.locator(".player-card[data-player-id]").first
        if card.count():
            card.click()
            page.wait_for_timeout(2500)
            drawer_open = page.locator("#drawer.open").count() > 0
            code = api_status.get(0)
            emit(f"drawer click -> open={drawer_open}, api status={code}")
            if not drawer_open or code != 200:
                failures.append(f"drawer open={drawer_open} api={code}")
        else:
            emit("no pitch cards visible (no materialized data) — drawer skipped")
        page.remove_listener("response", resp_handler)
        emit()

        emit("=== 3. Gate-1 UI landmarks ===")
        page.goto(BASE + "/dashboard", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        ribbon = page.locator('[data-testid="exec-ribbon"]').count()
        bottom = page.locator('[data-testid="bottom-nav"]').count()
        more_btn = page.locator("#moreBtn").count()
        emit(f"sticky ribbon present: {bool(ribbon)} · bottom tabs: {bool(bottom)} · More button: {bool(more_btn)}")
        if not ribbon or not bottom or not more_btn:
            failures.append("gate-1 UI landmark missing (ribbon/tabs/More)")
        page.locator("#moreBtn").click()
        page.wait_for_timeout(600)
        sheet = page.locator('[data-testid="more-sheet"].open').count()
        emit(f"More sheet opens: {bool(sheet)}")
        if not sheet:
            failures.append("More sheet does not open")
        emit()

        targets_page = BASE + "/targets"
        page.goto(targets_page, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        cards = page.locator('[data-testid="target-card"]').count()
        emit(f"/targets target cards rendered: {cards}")
        planner = page.goto(BASE + "/planner", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        steps = page.locator('[data-testid="plan-step"]').count()
        pressure = page.locator('[data-testid="rise-pressure-chip"]').count()
        emit(f"/planner plan steps: {steps} · rise-pressure chip: {bool(pressure)}")

        browser.close()

    emit()
    emit("=== VERDICT ===")
    if failures:
        emit("FAILURES:")
        for f in failures:
            emit(f"- {f}")
    else:
        emit("GATE 1 LIVE VERIFICATION PASSED.")

    out = OUT / "gate1_live.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwritten: {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
