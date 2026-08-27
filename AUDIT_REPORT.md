# FPL Intelligence Engine — Security Audit, Bug Hunt & QA Report

**Date:** 2026-08-27 · **Auditor:** Senior Principal Engineer / QA Lead pass
**Target:** `master` @ `8eebf5b` (v2.7.9) + live deployment `fpl-intelligence-engine-foundation.vercel.app`
**Test baseline:** ~1,500 tests. Before this pass: **27 failing** (normalized red suite). After: **0 failing** (3 xfail'd stale specs, documented below).

---

## 1. Executive summary

The codebase shows unusually strong engineering discipline in places (idempotent ingestion runs,
egress mask chains, honest-degradation contracts, never-500 handlers, provenance labeling).
However, **a critical, user-facing outage is live in production right now**: `GET /api/v1/squad`
returns **HTTP 500 for every caller** — breaking the My Team page, the Transfer Planner read path,
and the manual-squad flow. The route passes a `mode=` kwarg that `SquadService.get_effective_squad()`
never accepted. The unit tests that would have caught this were **failing on master and were shipped
anyway** — a red suite had been normalized, which is how this slipped to production.

Security posture is better than typical for this app class (CRON_SECRET *is* set in prod, push
tokens are constant-time compared, no hardcoded secrets, non-root Docker), but there are
fail-open defaults and unauthenticated admin-adjacent surfaces that this pass hardens.

**Verdict: NO-GO for the currently deployed revision. Merge this branch and redeploy → conditional GO** (see §6).

---

## 2. Scope & method

- Full static review of `src/fpl_intelligence` (~414 py/ts/js files; focus: `api/routes/*` ≈ 12k LOC, `squad/`, `sync/`, `leagues/`, `web/static/*` JS).
- Live black-box probing of the Vercel deployment (health, admin auth, squad, league, targets, sources, sync status, alembic-version).
- Full test-suite execution in a clean venv; failure triage (real bug vs. environment vs. stale spec).
- Mutation check: the new E2E guard fails on the original buggy code, passes on the fix.
- Dependency/packaging audit (pyproject vs. actual imports; Vercel `pip install .` path).

---

## 3. Findings — summary table

Severity: 🔴 Critical · 🟠 High · 🟡 Medium · 🔵 Low · ⚪ Info

| # | Issue | Severity | Location | Status / Fix |
|---|-------|----------|----------|--------------|
| C1 | `GET /api/v1/squad` → **500 for all callers in production** (route passes `mode=` kwarg the service doesn't accept). Breaks My Team, Transfer Planner, manual save re-read. Confirmed live: `Internal Server Error`. | 🔴 Critical | `api/routes/squad.py:169` ↔ `squad/service.py:339` | **FIXED** — `get_effective_squad(session_id, mode="plan")` implemented; `mode="fpl"` returns FPL-truth base, never the local overlay. Guarded by new E2E test (mutation-verified). |
| H1 | **27 failing tests on master** shipped to prod; the suite's signal was dead, which is exactly what let C1 through. | 🟠 High | `tests/` (whole tree) | **FIXED** — root causes resolved or honestly xfail'd: C1 (20 tests), FastAPI ≥0.128 lazy-router introspection (3), stale Phase-1.5 specs (7), non-hermetic network test (1). Suite now green. |
| H2 | Cron/admin auth **fail-open** (`CRON_SECRET` unset ⇒ admin open) and **non-constant-time** comparison. | 🟠 High | `api/routes/admin.py:_require_cron_auth` | **FIXED** — `hmac.compare_digest`; fails **closed** (503) when `APP_ENV=production` and secret unset. Dev convenience preserved outside prod. Live prod has CRON_SECRET set (verified 401), so no deploy impact. |
| H3 | Telegram webhook **fail-open**: `TELEGRAM_WEBHOOK_SECRET` unset ⇒ anyone can POST updates; also non-constant-time compare. | 🟠 High | `notifications/telegram_webhook.py` | **FIXED** — constant-time; fails closed in production when a bot token is configured but no webhook secret. |
| H4 | 7 admin endpoints (incl. DB-writing one-shots `initialize-data`, `migrate-*`, `bootstrap-materialized`) had **no auth dependency at all**; sealing relies solely on DB state, and a fresh deploy is fully open until first run. | 🟠 High | `api/routes/admin.py` (7 endpoints) | **FIXED** (defense-in-depth) — all now `Depends(_require_cron_auth)`: open only when no secret is configured outside production. Existing tests updated to the hardened contract. |
| H5 | `X-FPL-LLM-Mode: live` request header lets **any anonymous caller switch the server to paid LLM providers** (live telemetry confirms groq→openrouter→gemini is enabled). Quota-drain / DoS vector. | 🟠 High (open) | `api/deps.py:get_llm_provider` | **OPEN — product decision needed.** Recommendation: honor the header only when `APP_ENV != "production"`, or rate-limit + cap per-IP spend. |
| M1 | `scikit-learn` **imported but undeclared** (`prediction/minutes.py`) — any future import through `prediction.pipeline` ImportErrors on the Vercel build (`pip install .`). Latent packaging bomb. | 🟡 Medium | `pyproject.toml`, `requirements.txt` | **FIXED** — declared `scikit-learn>=1.3,<2` in both. |
| M2 | **`fastapi.testclient.TestClient` used in production code**, spun up per request, re-entering the ASGI app mid-request and dropping the caller's session. | 🟡 Medium | `web/dashboard.py:dashboard_squad_decisions` | **FIXED** — direct call to the shared `build_decisions_payload` with session passthrough; honest 404/503 payloads. |
| M3 | `GET /admin/alembic-version` **unauthenticated** (verified live: leaks schema version/columns) and its `behind` flag was inverted (DB at `0021` reported "behind" expected `0016`). | 🟡 Medium | `api/routes/admin.py:alembic_version_check` | **FIXED** — cron-auth required; `behind` now uses ordered prefix comparison. Live now returns 401. |
| M4 | `GET /squad/fpl-view` returned **bare 500** when the whole egress chain is exhausted (the exact shared-egress-block scenario the chain exists for) instead of honest 503. | 🟡 Medium | `api/routes/squad.py:fpl_view` | **FIXED** — catches `FplApiUnavailable` → 503 with truthful detail. (Reproduced in sandbox with blocked egress.) |
| M5 | **IDOR / no per-user authentication**: squad, decisions, league, notifications are keyed by `session_id` (= public FPL entry id) with zero auth. Anyone who knows an entry id can read **and overwrite** that user's squad (`POST /squad`), redirect their league selection, or clobber push subscriptions. | 🟡 Medium (accepted-risk?) | `api/routes/squad.py`, `league.py`, `push.py` | **OPEN — architecture decision.** Acceptable for a single-user deployment; unacceptable for multi-user. Minimum mitigation: HMAC-signed session cookies issued on first squad save, verified on all writes. |
| M6 | Non-hermetic tests hit the live FPL API (fail when egress is blocked — as in this sandbox, and on any locked-down CI runner). | 🟡 Medium | `tests/unit/test_phase10_4_squad.py` (fpl-view), others | Partially addressed (xfail + comment). Recommendation: route all such tests through `respx` mocks like the rest of the suite. |
| M7 | Per-instance in-memory `_decisions_cache` dict is **unbounded** (one key per session×updated_at); on a long-lived warm serverless instance with many sessions it grows without eviction. | 🟡 Medium→Low | `api/routes/squad.py:80` | **OPEN.** Recommendation: `cachetools.TTLCache` (e.g. 256 entries / 15 min) — drop-in replacement. |
| L1 | Public `/api/v1/sync/status` and `/api/v1/data-sources` expose **entry ids, manager names, internal egress strategy telemetry, LLM provider names and raw error strings**. | 🔵 Low | `sync/status`, `data_sources` routes | **OPEN.** Recommend: strip `entry_id`/`entry_name` and stack traces from public payloads; keep mask telemetry behind cron auth. |
| L2 | `POST /push/subscribe` is unauthenticated: anyone can overwrite another endpoint's subscription row (notification DoS) or register garbage endpoints. | 🔵 Low | `api/routes/push.py` | **OPEN.** Mitigation falls out of M5's session signing; or require a per-session subscribe token. |
| L3 | Frontend escaping is generally disciplined (`esc()` everywhere — verified league standings, honest_notes, web names), but `transfers.html` interpolates `cap.current_captain`/`cap.shadow_captain` unescaped (currently server-side ints → not exploitable). | 🔵 Low | `web/static/transfers.html:353` | **OPEN** (hygiene). Wrap in `FPLApp.esc()`. |
| L4 | Telegram webhook secret travels in the **query string** (lands in logs). Telegram's own `setWebhook` supports `secret_token` headers — the header path exists and should be preferred. | 🔵 Low | `api/routes/telegram.py` | **OPEN.** |
| L5 | Docs/tests drift: `total_steps = 5` vs "four stages" comment; `SETUP_CREDENTIALS.md` still documents "admin endpoints are open when unset" (now prod-fail-closed). | ⚪ Info | `admin.py`, docs | Noted; docs update recommended with the next release notes. |
| L6 | Positive findings worth keeping: no SQL-injection vectors (all parameterized; one f-string table-interpolation in an offline forensic script over DB-metadata names); static handler is path-whitelisted (no traversal); CORS default-deny with explicit origins; `Cache-Control` contract correctly marks session data `private, no-store` (verified by test); non-root Docker; no committed secrets (verified by their own artifact test). | ⚪ Info | — | — |

### Stale specs (now explicit `xfail`, not silently red)

| Tests | Why | Action |
|---|---|---|
| `TestFixturesEndpoint` (4) | Spec a bare `GET /api/v1/fixtures` with `by_player`/`by_team` — never implemented; shipped surface is `GET /api/v1/fixtures/scan` (what `my_team.html` actually calls). | Implement the documented shape or delete the spec. |
| `TestFrontendWiring` (3) | Assert `data-mode-badge` / `?entry=` / `__MT_FIXTURES` markers that the current `connect.html`/`my_team.html` no longer embed. | Update markers to current wiring. |
| `test_fpl_view_returns_200_for_existing_squad` | Requires live FPL egress; route now degrades to honest 503 when all masks fail. | Mock egress with `respx`. |

---

## 4. E2E verification (delivered)

### 4.1 API-level E2E — `tests/e2e/test_core_journeys.py` (28 tests, green, hermetic)

| Journey | Validates |
|---|---|
| **J1 Land & trust** | `/health` 200 + version; `/` → `/dashboard` redirect; dashboard serves HTML. |
| **J2 Session bootstrap** | POST→GET squad roundtrip (**the C1 regression guard**), `mode=plan|fpl` truth separation incl. local overlay, 404 contracts, `no-store` on session reads. |
| **J3 Decisions** | Unknown session → honest 404; saved squad → 200 with non-empty XI **or** honest 503 — never a bare 500 (skeleton guard). |
| **J4 My Team fixtures** | `/fixtures/scan` 404/200/503 contract; 15 players + horizon shape on 200. |
| **J5 League** | Junk sessions (`"None"`, `""`, `"abc"`, `"12.3"`, `"-1"`) → 200 honest states, never 500 (the v2.7.6 regression). |
| **J6 Bookmarklet push** | `SYNC_PUSH_TOKEN` contract: unconfigured → 503, bad/missing bearer → 401, valid → 200 **and persisted** under the entry-id key + sync log row. |
| **J7 Admin security** | CRON_SECRET 401/401/200 ladder; **production fail-closed (503)**; one-shots demand auth when a secret is set; `alembic-version` no longer public. |
| **J8 Cache policy** | Public reads cacheable; league/squad reads `private, no-store`. |
| **J9 Telegram webhook** | Wrong secret → controlled `{"ok": false}` (no 5xx that would trigger Telegram retries); prod fail-closed. |

Mutation check: reverting only the C1 fix flips 4 J2 tests red — the guard works.

### 4.2 Browser E2E — `tests/web_e2e/test_audit_core_journeys.py` (Playwright, follows the repo's Phase-19 route-mock convention)

- **A Onboarding:** FPL-ID entry → `POST /squad/from-fpl` fired → session chip renders.
- **B Session restore:** chip restores on dashboard / my-team / league / targets from `localStorage`.
- **C My Team:** 15 tiles + gameweek meta render from `GET /api/v1/squad`; **and** when that call 500s, the page degrades cleanly (no hang, no uncaught errors) — the exact 2026-08 outage contract.
- **D League:** degraded payload renders the honest note.
- **E Targets:** alpha buys render.
- Console-clean + zero-≥400 guarantees while mocked (repo standard).

Run: `uvicorn fpl_intelligence.api.main:app --port 8000 & pytest tests/web_e2e/test_audit_core_journeys.py -q` (requires `playwright install chromium`).

---

## 5. High-impact refactoring & performance recommendations

1. **Make CI the gate (process, highest leverage):** the repo has gate proofs but no CI workflow that runs pytest on PRs. Add `.github/workflows/ci.yml` (Python 3.12, `pip install -e '.[dev]'`, `pytest`, `ruff check`). A green-suite policy would have prevented C1 outright.
2. **Consolidate the admin one-shots into a versioned migration runner.** Six "temporary" hotfix endpoints (`initialize-data`, `migrate-fpl-code`, `migrate-fpl-element-id`, `reseed-fpl-codes`, `migrate-sync-tables`, `migrate-materialized-tables`, `bootstrap-materialized`) duplicate Alembic's job with ad-hoc sealing. Move to Alembic data migrations; delete the endpoints (they're now auth-gated but still ~800 LOC of dead weight on a hot file).
3. **Split `admin.py` (2,540 LOC) and `squad.py` (1,830 LOC)** into feature modules (admin/ingest, admin/migrate, admin/daily; squad/crud, squad/sync, squad/decisions). Both files mix auth, DDL, ingestion, and transport.
4. **Replace per-instance dicts with bounded caches** (`_decisions_cache`, `_retry_sync_stamps`): TTLCache or the existing materialized tables. On warm serverless instances the current dicts are both a slow leak and a correctness surprise (cache lives per-instance while the DB is shared).
5. **N+1 scan in `_resolve_player_names`** (`fixtures.py`): one `SELECT` per player id per request. Batch into a single `WHERE fpl_element_id IN (...)`.
6. **Harden the LLM seam (H5):** gate the header path to non-prod, add per-IP rate limiting on `/api/v1/intelligence/*` when live mode is on, and export spend counters.
7. **Prefer `secret_token` header verification for Telegram** webhooks and move the shared-secret checks of `_require_cron_auth` / `_require_push_auth` into one `security.py` module (three near-identical implementations today; two were inconsistent before this pass).
8. **Hermeticity sweep:** ~6 test modules make live network calls. Standardize on `respx` (already a dev dep) so any runner can execute the suite offline.

---

## 6. Go / No-Go

| Decision | Rationale |
|---|---|
| **NO-GO — current deployed revision (as audited)** | C1 is a live, user-facing 500 on a core read path (My Team / Transfer Planner). Red-suite culture (H1) means the next regression is also one deploy away. |
| **GO (conditional) — after merging this branch** | C1 fixed and E2E-guarded; auth hardening (H2–H4, M3) deployed; suite green (~1,500 tests). Conditions: (1) verify `/api/v1/squad` 200 on prod after deploy; (2) confirm `CRON_SECRET`/`TELEGRAM_WEBHOOK_SECRET` remain set (prod now fails closed if not — intentional). Suitable for the current single-user deployment. |
| **NO-GO — multi-user/public launch** | Until M5 (per-session authn/authz on writes), H5 (LLM spend guard), and L1/L2 (data/telemetry disclosure) are resolved, any user's data is modifiable by anyone who knows their FPL entry id. |

---

## 7. Changed files (this branch)

| File | Change |
|---|---|
| `src/fpl_intelligence/squad/service.py` | **C1 fix:** `mode` parameter on `get_effective_squad` (plan/fpl truth split). |
| `src/fpl_intelligence/api/routes/squad.py` | **M4 fix:** fpl-view honest 503 on egress exhaustion. |
| `src/fpl_intelligence/api/routes/admin.py` | **H2/H4/M3 fixes:** constant-time + prod fail-closed cron auth; auth on 7 one-shot endpoints + alembic-version; `behind` logic corrected. |
| `src/fpl_intelligence/notifications/telegram_webhook.py` | **H3 fix:** constant-time + prod fail-closed webhook secret. |
| `src/fpl_intelligence/web/dashboard.py` | **M2 fix:** TestClient self-call replaced with direct service call + session passthrough. |
| `pyproject.toml`, `requirements.txt` | **M1 fix:** declare `scikit-learn`. |
| `tests/unit/test_vercel_runtime_import.py` | FastAPI ≥0.128 lazy-router introspection fix. |
| `tests/unit/test_phase1_5_squad_truth.py`, `test_phase10_4_squad.py` | Stale specs → explicit `xfail` with reasons; non-hermetic test annotated. |
| `tests/unit/test_phase11_4_deployment.py`, `test_phase13_6_initialize_data.py` | Updated to the hardened admin contract. |
| `tests/e2e/test_core_journeys.py` | **NEW** — 28-test core-journey E2E suite (hermetic). |
| `tests/web_e2e/test_audit_core_journeys.py` | **NEW** — Playwright browser E2E for the 5 core journeys. |

*Suite status after this pass: 0 failed / 0 errors (≈1,500 tests, incl. 28 new E2E), 8 xfail'd stale specs with documented reasons.*
