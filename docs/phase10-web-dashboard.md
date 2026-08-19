# Phase 10.3 — Web Dashboard

## Status

**INITIALIZED** (2026-08-19). Adds a simple single-page web dashboard that
consumes the Phase 10.1 REST API to display FPL intelligence without requiring
Telegram or programmatic API access.

## Delivered

1. **`src/fpl_intelligence/web/dashboard.py`** — FastAPI router that serves
   `dashboard.html` at `GET /dashboard`.
2. **`src/fpl_intelligence/web/static/dashboard.html`** — Single-page app
   (vanilla JS, no build step) that fetches:
   - `GET /api/v1/health` — system health + metrics
   - `GET /api/v1/intelligence/player/{id}` — player intelligence report
   - `GET /api/v1/intelligence/unresolved` — unresolved evidence triage table
3. **`tests/unit/test_phase10_3_web.py`** — 3 tests covering route response,
   static file existence, and API references in HTML.
4. **Wired into `src/fpl_intelligence/api/main.py`** — dashboard router included
   alongside the Phase 10.1 intelligence router.

## Constraints honoured

- Does not modify Phases 1–8.
- Does not modify Phase 9 core logic.
- No hardcoded secrets.
- No live API calls in tests.

## Verification

- `ruff` clean on all new modules.
- `mypy` clean on all new modules.
- All Phase 10.3 tests pass.

## Next

Phase 10.4 is unblocked.

### Phase 10.4 — Squad Decisions Integration

The dashboard now includes a **Squad Decisions** section that proxies
`GET /api/v1/decisions` and displays the optimized Starting XI, Bench Order,
Captain recommendation, Transfer Plan, and Chip recommendation from the Phase
10.4 `DecisionOptimizerBridge`.

The dashboard router was extended with a
`GET /api/v1/dashboard/squad-decisions` proxy endpoint that calls the
decisions API and returns the JSON result for the SPA.
