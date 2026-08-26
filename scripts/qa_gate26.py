import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = os.getenv("QA_BASE", "https://fpl-intelligence-engine-foundation.vercel.app")
OUT = Path("qa_gate26")
OUT.mkdir(exist_ok=True)

viewports = [(390, 844, "390"), (768, 1024, "768"), (1440, 900, "1440")]
pages = [
 ("/", "Decisions"),
 ("/my-team", "MyTeam"),
 ("/live", "Live"),
 ("/league", "League"),
 ("/track-record", "TrackRecord"),
 ("/targets", "Targets"),
]

console_errors = []
failed = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(ignore_https_errors=True)
    for path, label in pages:
        for w, h, name in viewports:
            page = ctx.new_page()
            page.on("console", lambda m, lab=label, nm=name: console_errors.append(f"{lab} {nm} console:{m.type} {m.text}") if m.type=="error" else None)
            page.on("pageerror", lambda e, lab=label, nm=name: console_errors.append(f"{lab} {nm} pageerror: {e.message}"))
            page.set_viewport_size({"width": w, "height": h})
            url = BASE + path
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(1500)
                file = OUT / f"{label}_{name}.png"
                page.screenshot(path=str(file), full_page=True)
                print(f"ok {label} {name} -> {file}")
                has_h = page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 2")
                if has_h:
                    failed.append(f"{label} {name} horizontal scroll")
                    print(f"  WARN h-scroll {label} {name}")
                if w == 390:
                    small = page.evaluate("""() => {
                      const els=[...document.querySelectorAll('button, a')].filter(el=>{
                        const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&(r.width<44||r.height<44)&&getComputedStyle(el).display!=='none';
                      }); return els.slice(0,3).map(e=>e.tagName+'.'+e.className.slice(0,40)+' '+Math.round(e.getBoundingClientRect().width)+'x'+Math.round(e.getBoundingClientRect().height));
                    }""")
                    if small:
                        print(f"  small targets {label}: {small}")
            except Exception as e:
                print(f"FAIL {label} {name}: {e}")
                failed.append(f"{label} {name}: {e}")
            page.close()
    browser.close()

print("\n=== console ===")
if not [e for e in console_errors if "error" in e.lower()]:
    print("console 0: PASS (no error/pageerror)")
else:
    for e in console_errors: print(e)
    failed.append(f"console {len(console_errors)}")

print("\n=== h-scroll ===")
hs=[f for f in failed if "horizontal" in f]
if not hs: print("no h-scroll PASS")
else: print("\n".join(hs))

OUT.joinpath("qa.json").write_text(json.dumps({"console":console_errors,"failed":failed,"base":BASE}, indent=2))
print(f"\nQA done {OUT}/ failed={failed}")
if failed:
    raise SystemExit(1)
