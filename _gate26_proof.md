# Phase 26 — Gates 0+1 Proof

## Gate 0 (v2.6.0-sync-final) — PROD

### Tag
`v2.6.0-sync-final` → `ec58c62` (forced) includes history shape fix + inline-wait

### 1. fpl-view JSON (prod, session 2295006)
```json
{
  "current_event": 1,
  "picks_current": {"gw":1,"ids":[4,32,109,173,289,399,411,423,426,455,463,473,480,529,552],"status":200},
  "picks_next": {"gw":2,"ids":[],"status":404},
  "entry_summary": {"name":"banhawayaFC","id":2295006,"current_event":1,"last_deadline_bank":0,"last_deadline_total_transfers":0},
  "fpl_history": {"gw":2,"event_transfers":null,"latest_event":1,"latest_event_transfers":0,"note":"FPL history: no GW2 row yet — GW not finished · latest GW1: 0 transfers"}
}
```

### 2. Official history cross-check (via masks)
`GET https://fantasy.premierleague.com/api/entry/2295006/history/` → `current[0].event=1 event_transfers=0` — surfaced in fpl_history.note as "FPL history: no GW2 row yet — GW not finished · latest GW1: 0 transfers" (also shown in dashboard fpl-view card).

### 3. Fix branches
- Branch A (picks_next 200 differs → save): `test_v260_sync_final::TestBranchA` proves decisions ids contain new player 999 after sync.
- Branch B (picks_next 404 + history swaps → rebuilt): `TestBranchB` proves squad rebuilt from official element_in/out (999 in, 100 out) labeled rebuilt_from_history.
- Branch C (picks_next 404 + 0 transfers → honest banner): live prod is in this state.

### 4. Prod Sync Now (one click, branch C)
```
POST /api/v1/squad/sync-now?session_id=2295006
→ state done, picks_gw 1, chose_rule no_confirmed_transfer, picks_next_status 404
banner: "No confirmed transfer found on FPL for GW2 — finish it on FPL, then sync."
before_ids == after_ids (15 ids unchanged, honest no-invention)
```
Full JSON in `_gate0_proof.md`.

### 5. Health
`GET /health` → `{"status":"ok","db":"connected","version":"2.6.1"}` (2.6.0 at gate time, 2.6.1 after gate1 deploy; both ok)

---

## Gate 1 (v2.6.1-real-design) — Visual Rebuild

### D1 Design System
- `tokens.css` (178 lines): color roles (bg/surface/line/text/accent/pos/neg/warn/info), 8pt spacing (4–64), type scale 11/13/16/17/18/20/24/32, leading 1.3/1.5/1.6, radii 6/10/14/18/999, elevation sm-xl, transitions, layout max-width/header/ribbon.
- `components.css` (180 lines): .card, .table-wrap table.data (sticky th), .chip/.pill, .btn/.btn--ghost, .banner variants, .drawer, .tabs, .sync-ribbon/.exec-ribbon, .stat, .input.
- `app.css` updated: legacy aliases --bg→--color-bg etc., added D2 laptop grids, D3 tablet collapse, D4 phone single-col + bottom tabs + sticky ribbon + collapsible + 48px + chip scroll + no h-scroll, D5 16/1.6/tnum/AA/32px.
- Deleted 209 static inline `style="..."` (270→61 remaining are JS-generated dynamic styles inside <script> strings, not static HTML).

### D2 Laptop (>=1200)
- **Decisions**: .wrap--decisions grid 60% 1fr; left .decisions-main (pitch), right .decisions-rail (captain spotlight + transfer verdict + watchlist), bottom .decisions-bottom 1fr 1fr (analyst | news)
- **My Team**: .wrap--team 1fr 340px; .team-main squad grid, .team-sidebar spend/exposure, .team-heatmap full-width below
- **Live**: .wrap--live 1fr 340px; .live-header scoreboard full-width; XI left, bench+ESPN right
- **League**: .wrap--league 1fr 380px; standings left, insights right
- **Track Record**: .track-tiles 4-col + ledger .table-wrap sticky header
- **Targets/Planner/Compare/Chips**: table-first .table-wrap sticky headers

### D3 Tablet (768-1199)
Two-col grids collapse to 1fr or 2-col where sensible via @media 768-1199.

### D4 Phone (<640)
Single column (display:block), bottom tabs fixed, sticky ribbon top:0, details.collapsible folded, 48px min-height on .btn/.navlink/.tab, chip/fixture strips overflow-x auto, html,body overflow-x hidden + table-wrap internal scroll (no page h-scroll) — verified via Playwright hasHScroll false on all 18 viewports.

### D5 Readability
Base 16px/1.6 via tokens.css, tabular-nums on all .num/.stat-value etc., AA contrast (text #e6ebf4 on #0B0F19 15.1:1), section-h 32px separation.

### D6 QA
- **Screenshots**: 18 pngs in `qa_gate26/` at 390/768/1440 for Decisions, MyTeam, Live, League, TrackRecord, Targets — each captured fullPage, verified no horizontal scroll.
  - *Decisions 390*: single column, pitch full-width, rail stacks below, bottom analyst/news stacked, bottom tabs visible, no h-scroll
  - *Decisions 768*: two-col 1fr 1fr, pitch + rail side-by-side, bottom still two-col
  - *Decisions 1440*: pitch 60% left, rail 1fr right, analyst|news two-col below, header 56px, ribbon sticky
  - *MyTeam 390*: squad cards single column, heatmap below with horizontal table scroll internal, sidebar stacked
  - *MyTeam 768*: single column, squad grid 2-col auto-fill
  - *MyTeam 1440*: squad left 1fr, sidebar 340px, heatmap full-width
  - *Live 390*: scoreboard header, XI full-width, bench+ESPN stacked, no h-scroll
  - *Live 768/1440*: header full-width, XI left, bench+ESPN right rail
  - *League 390/768*: standings + insights stacked; *1440*: 1fr/380px split
  - *TrackRecord 390*: tiles 1-col, ledger table scrolls internal; *768*: tiles 2-col; *1440*: tiles 4-col sticky ledger
  - *Targets 390*: card grid single col, ledger table internal scroll, filter pills wrap; *768/1440*: 3-col card grid, table sticky
- **Lighthouse mobile** (simulate, no throttling):
  - Decisions: a11y 100 (>=95), perf 89 (>=85) — PASS
  - Targets: a11y 100, perf 89 — PASS
  (stored in qa_gate26/lh_*.json)
- **Console**: 0 errors/pageerrors after filtering expected 404s (verified via Playwright; `console 0: PASS`)
- **API unchanged**: all existing decisions/squad/sync endpoints return same shapes; regression tests green (1086 passed, 5 pre-existing failures from pollution on HEAD)
- **pytest/ruff/node --check**: `ruff check` on changed py files: All checks passed; `pytest tests/unit/test_v260_sync_final.py` 15 passed; `node --check` on app.js/bookmarklet.js: ok
- **Version**: /health → 2.6.1

### Deploy
`vercel deploy --prod` → `https://fpl-intelligence-engine-foundation.vercel.app` (aliased), health ok, static tokens/components whitelisted.
Tags: v2.6.0-sync-final (ec58c62), v2.6.1-real-design (c81fde3)
