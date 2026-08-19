# Phase 10.1 — FastAPI Intelligence API

Exposes the Phase 9 Live Intelligence Accumulator and AI Analyst over HTTP, so
dashboards, bots and mobile apps can consume live intelligence without touching
the quantitative Phases 1–8 stack.

The router lives at `src/fpl_intelligence/api/routes/intelligence.py` and is
mounted on the Phase 1 `FastAPI` app (`src/fpl_intelligence/api/main.py`) under
the prefix `/api/v1`.

---

## Live vs Mock LLM mode (important)

The LLM provider injected into every endpoint **defaults to the deterministic
`MockLLMProvider`**. This means:

* no network calls are made;
* no API quota is consumed;
* output is reproducible and safe to exercise in tests.

A real provider is built **only** when the caller explicitly opts in through
one of:

* the environment variable `FPL_API_USE_LIVE_LLM=true`, **or**
* the request header `X-FPL-LLM-Mode: live`.

When a real provider is requested but credentials are missing/unavailable, the
endpoint returns `503 Service Unavailable` with a clear message. **No API keys
are hardcoded anywhere** — the real provider reads configuration from the
git-ignored `.env` / environment, exactly like the Phase 9 CLI.

```bash
# Default (mock) — safe for tests / local dev
curl http://localhost:8000/api/v1/intelligence/player/4?gameweek=3

# Live LLM (only if you have credentials configured)
FPL_API_USE_LIVE_LLM=true uvicorn fpl_intelligence.api.main:app
# or, per-request:
curl -H "X-FPL-LLM-Mode: live" http://localhost:8000/api/v1/intelligence/player/4?gameweek=3
```

---

## Endpoints

### `GET /api/v1/health`

Phase 9.8 deployment health status and metrics.

* Probes database connectivity (read-only `SELECT 1`).
* Surfaces the shared Phase 9.8 `MonitoringService` metric/health snapshot.

**Response (200):**

```json
{
  "status": "ok",
  "version": "0.1.0",
  "phase": "10.1",
  "deployment_tag": "v0.9.8-production-deployment",
  "phase9_8_deployment": { "tag": "v0.9.8-production-deployment", "status": "closed" },
  "monitoring": {
    "metrics": { "metrics": [ { "name": "...", "kind": "counter", "value": 1.0 } ] },
    "health": { "all_ok": true, "summary": "ok=2 degraded=0 down=0", "checks": [ ... ] },
    "alerts_fired": 0
  }
}
```

`status` is `"ok"` when every registered component health check passed,
otherwise `"degraded"`.

---

### `GET /api/v1/intelligence/player/{player_id}`

Generates an `IntelligenceReport` for one player using the Phase 9.4
`AnalystReportGenerator` (which reads the quantitative engine read-only via
`PredictionContextBuilder` and queries pre-deadline evidence via
`EvidenceQueryService`).

**Query parameters:**

| name      | type | required | description                                                              |
|-----------|------|----------|--------------------------------------------------------------------------|
| `gameweek`| int  | no       | FPL gameweek number (defaults to `1`).                                  |
| `cutoff`  | str  | no       | ISO-8601 cutoff time; only pre-deadline evidence is included (default now). |
| `format`  | str  | no       | `md` to receive Markdown instead of JSON.                               |

**Headers:** `Accept: text/markdown` also selects Markdown output.

**Response (200, JSON):** the `IntelligenceReport` model —
`task`, `headline`, `prediction_context` (player id, gameweek, expected points,
minutes, start probability, floor, ceiling), `qualitative_adjustment`,
`net_assessment`, `recommendation`, `confidence`, `confidence_band`, `citations`,
`unresolved_warnings`, `caveats`, `is_mock`, etc.

**Response (200, Markdown):** `Content-Type: text/markdown` rendering of the
same report (see `IntelligenceReport.render_markdown()`).

```bash
curl "http://localhost:8000/api/v1/intelligence/player/4?gameweek=3&format=md"
```

---

### `POST /api/v1/ingest`

Ingests raw text through the Phase 9.2 `ingest_raw_text` pipeline
(hash → dedup → ledger → extract → persist evidence).

**Request body:**

```json
{
  "source_id": "press_conference_manual",
  "content_text": "The manager confirmed a 4-4-2 formation for the next match.",
  "published_at": "2025-08-10T10:00:00+00:00",
  "url": "https://example.com/report",
  "external_id": null,
  "title": null
}
```

| field          | type   | required | notes                                         |
|----------------|--------|----------|-----------------------------------------------|
| `source_id`    | string | yes      | Phase 9.2 source identifier (auto-registered).|
| `content_text` | string | yes      | Raw unstructured content.                     |
| `published_at` | string | yes      | ISO-8601, **timezone-aware** (422 if invalid).|
| `url`          | string | no       | Source URL.                                   |
| `external_id`  | string | no       | Provider-side id.                             |
| `title`        | string | no       | Display title (defaults to source/url).      |

**Response (200):** the `ManualIngestReport` summary —

```json
{
  "status": "created",
  "source_id": "press_conference_manual",
  "content_hash": "…",
  "raw_item_id": 1,
  "extraction_run_id": 1,
  "availability_count": 0,
  "tactical_count": 1,
  "resolved_count": 1,
  "unresolved_count": 0,
  "ambiguous_count": 0,
  "availability_evidence_ids": [],
  "tactical_evidence_ids": [1],
  "unresolved_evidence_ids": [],
  "duplicate": false,
  "error": null
}
```

`status` is one of `created` / `duplicate` / `rejected`. Re-submitting identical
text from the same source is idempotent (`duplicate`).

---

### `GET /api/v1/intelligence/unresolved`

Paginated list of `UnresolvedLiveEvidence` (Phase 9.2.1) rows for human triage
and manual entity mapping.

**Query parameters:**

| name   | type | required | description                       |
|--------|------|----------|-----------------------------------|
| `limit`| int  | no       | Page size, 1–500 (default 50).   |
| `offset`| int | no       | Offset, ≥ 0 (default 0).          |

**Response (200):**

```json
{
  "items": [
    {
      "id": 1,
      "raw_item_id": 1,
      "source_id": 1,
      "extraction_run_id": 1,
      "evidence_type": "availability",
      "player_name": "Unknown Striker",
      "team_name": null,
      "team_hint": null,
      "status_mentioned": "out",
      "quote": "the striker is ruled out",
      "confidence": 0.9,
      "resolution_status": "unresolved_player",
      "resolution_reason": "no canonical player match",
      "created_at": "2025-08-10T10:00:00+00:00"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

---

## Dependency injection

All seams are injected via FastAPI `Depends` in
`src/fpl_intelligence/api/deps.py`:

* `get_db` — the SQLAlchemy `Session` (overridable in tests).
* `get_llm_provider` — `MockLLMProvider` by default, real provider on opt-in.
* `get_prediction_provider` — `StaticPredictionProvider` (offline) by default;
  override with a real `DecisionPredictionProvider` in production.
* `get_prediction_builder` — `PredictionContextBuilder`.
* `get_evidence_service` — `EvidenceQueryService` (real evidence only).
* `get_analyst` — `AIAnalyst`.

In tests, override `deps.get_db`, `deps.get_llm_provider` and
`deps.get_prediction_provider` with `app.dependency_overrides` (see
`tests/unit/test_phase10_api.py`).

## Quality gates

* `ruff` clean on `src/fpl_intelligence/api`.
* `mypy` clean on `src/fpl_intelligence/api`.
* 7 offline `TestClient` tests (`tests/unit/test_phase10_api.py`) cover all four
  endpoints; the full suite passes with the new tests included.
* No quantitative Phases 1–8 code is modified.
