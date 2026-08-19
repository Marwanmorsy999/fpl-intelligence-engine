# Phase 11.1 — API-First Structured Data Integration

The engine pivots toward an **API-first production architecture**. Core facts
(injuries, chance of playing, confirmed lineups, formations) now flow from
structured public football/FPL APIs. The Phase 9/10 LLM layer remains available
as an *optional analyst layer* but is **not required** for core
decision-making.

This layer is additive and respects every Phase 1–8 invariant:

* No Phase 9/10 LLM code was deleted or modified.
* No Phase 1–8 quantitative algorithm was modified.
* No API keys are hardcoded (environment only).
* No paid API is required.
* No live API call is made inside `pytest` (all connectors use a mocked
  `httpx.MockTransport`; tests feed fixture JSON).

---

## Supported APIs

| API | Module | Key required? | What it provides |
| --- | ------ | ------------- | ---------------- |
| Official FPL | `fpl_official.py` | **No** | `bootstrap-static` (news, `chance_of_playing`, `status`, `expected_minutes`, price, team), `fixtures` (difficulty), `element-summary/{id}`. |
| API-Football (v3) | `api_football.py` | **Yes** (`API_FOOTBALL_KEY`) | Fixtures by date, confirmed lineups by fixture (starting XI + bench), injuries. |
| football-data.org (v4) | `football_data_org.py` | **Yes** (`FOOTBALL_DATA_ORG_KEY`) | Competitions, matches, standings. Enriches match context; no per-player availability feed. |

Each connector extends `BaseDataConnector`, which provides cache-first HTTP
(`_get_json`), polite rate limiting (via the Phase 9.1 `RateLimiter`), and typed
errors (`DataConnectionError`, `DataParseError`, `DataProviderDisabledError`).

---

## Required / Optional Environment Variables

| Variable | Required? | Used by | Behaviour if missing |
| -------- | --------- | ------- | -------------------- |
| `API_FOOTBALL_KEY` | Optional | `ApiFootballConnector` | Connector disables itself gracefully; `is_enabled()` returns `False`; a clear warning is logged once at construction. |
| `FOOTBALL_DATA_ORG_KEY` | Optional | `FootballDataOrgConnector` | Connector disables itself gracefully; `is_enabled()` returns `False`; a clear warning is logged once at construction. |

The official FPL API needs **no key** and is always enabled. No variable is
ever hardcoded; connectors read keys from the environment (or a constructor
override for tests). `fetch_live_facts.py` reads these from the environment only
and never persists them.

---

## Free-Tier Limits (no paid APIs)

* **Official FPL** — public, unauthenticated, no documented hard quota but be
  polite (the connector paces with a 1s minimum interval by default).
* **API-Football** — free tier is ~100 requests/day (verified against the
  provider's published limits at the time of writing; confirm before heavy
  use). The connector caches aggressively (see Caching Policy) so a single run
  issues far fewer live calls.
* **football-data.org** — free tier is 10 requests/min. The connector caches
  and paces; it contributes match/standings context only.

Because the keyed connectors degrade to "no facts" when their key is absent,
the engine runs fully on the keyless official FPL feed alone.

---

## Caching Policy

`ResponseCache` (cache.py) is the single caching boundary. It is **cache-first**:
a cache hit returns immediately and never reaches the network.

* **Key:** `endpoint` + normalised, sorted `params` (stable string).
* **TTL tiers:**
  * *General data* (bootstrap-static, fixtures): **15 minutes** default.
  * *Deadline-sensitive data* (confirmed lineups, team news, injuries):
    **1 minute** default.
* **In tests:** the cache is in-memory and connectors receive a mocked
  `httpx.MockTransport`, so no network is ever touched. The CLI
  (`fetch_live_facts.py`) may pass `--cache-dir` to persist the cache to
  `data/cache/live_facts/data_provider_cache.json` and reuse results across runs.
* **Never hammer APIs:** the rate limiter pauses *before* each request; the
  cache means repeat fetches (e.g. many lineups for one matchday) collapse to
  one stored value.

TTLs are configurable: `ResponseCache(default_ttl_seconds=..., sensitive_ttl_seconds=...)`
or via `store(..., sensitive=True)`.

---

## Fact Override Rules

`LiveFactInjector` converts structured `PlayerFact` rows into hard
`FactOverride` objects. Only the fields that are actually overridden are set;
the decision layer leaves any `None` field at its baseline value.

| Condition | Override produced |
| --------- | ----------------- |
| Official FPL `chance_of_playing == 0` | `start_probability = 0.0` |
| Official FPL `chance_of_playing == 100` | `start_probability = 1.0` |
| Official FPL `news` contains `"suspended"` | `availability_status = "suspended"` |
| API-Football confirmed in **starting XI** | `start_probability = 1.0`, `expected_minutes = 90` |
| API-Football confirmed on **bench** | `start_probability = 0.0`, `expected_minutes = 0` |
| API-Football **injured / out** | `start_probability = 0.0`, `availability_status = "out"` |

**Source precedence** (when two facts target the same FPL player id):
API-Football (confirmed lineup) > Official FPL > football-data.org. A later,
higher-priority source wins for overlapping fields.

**Application:** `FactOverrideProvider` wraps the baseline quantitative
`DecisionPredictionProvider`. It fetches the baseline prediction and
post-processes only the public fields — `start_probability`, `expected_minutes`,
and a proportional rescale of `expected_points` (so a start probability of 0 or
zero minutes drives expected points to 0). The upstream Phase 1–8 model is
**never mutated**; a new `PlayerPrediction` is returned.

**ID mapping:** API-Football / football-data.org facts carry an
`api_football_player_id`; an optional `fpl_id_map` (owned by the Phase 9.2.1
entity-resolution layer) maps them to FPL player ids. Facts without a resolvable
FPL id are skipped by the injector until mapping is available.

---

## Fallback Behaviour

The integration is designed so a missing key, a network outage, or a parse
failure **never fails the request**:

1. **Disabled keyed connector** → `collect_player_facts()` returns `[]`; the
   orchestrator records `enabled: False` in diagnostics and proceeds with the
   facts it has (official FPL).
2. **Network / parse error for one source** → caught per-source in
   `LiveFactInjector.inject_from_connectors`; the error is recorded in
   diagnostics and other sources still contribute overrides.
3. **`GET /api/v1/decisions?live_facts=true` fails** (any exception while
   collecting) → the endpoint logs a warning and falls back to the baseline
   quantitative predictions; the response still succeeds (HTTP 200) with
   `meta.live_facts_applied == 0`.
4. **`live_facts=false` (default)** → baseline predictions only, no external
   calls at all.

---

## Files

* `src/fpl_intelligence/data_providers/cache.py` — `ResponseCache`.
* `src/fpl_intelligence/data_providers/facts.py` — `PlayerFact`, `FactOverride`,
  `FactSource`, `FactConfidence`.
* `src/fpl_intelligence/data_providers/base.py` — `BaseDataConnector` + errors.
* `src/fpl_intelligence/data_providers/fpl_official.py` — Official FPL connector.
* `src/fpl_intelligence/data_providers/api_football.py` — API-Football connector.
* `src/fpl_intelligence/data_providers/football_data_org.py` — football-data.org connector.
* `src/fpl_intelligence/data_providers/live_fact_injector.py` — `LiveFactInjector`.
* `src/fpl_intelligence/data_providers/decision_bridge.py` — `FactOverrideProvider`,
  `FactCollectionService`.
* `src/fpl_intelligence/api/routes/squad.py` — `GET /api/v1/decisions` bridge.
* `scripts/fetch_live_facts.py` — CLI (`--dry-run`, `--cache-dir`, `--date`).
* `tests/unit/test_phase11_data_providers.py` — 69 tests.

---

## Closure Verification (2026-08-19)

* Full suite: **841 passed** (was 772; +69 Phase 11.1 tests; > 772 bar met).
* `ruff` clean on all Phase 11.1 modules.
* `mypy` clean on all Phase 11.1 modules.
* No migration required (no new tables/columns/enums).
* **Not classified A/B/C** — integration/architecture layer, not an empirical
  evaluation of the prediction stack.
