# Phase 9 Architecture — Live Intelligence Accumulator & LLM Analyst Layer

**Status:** IMPLEMENTATION_COMPLETE
**Last updated:** 2026-08-19 (Phase 9.7 — Live End-to-End Verification initialized; Phase 9.6 — Scheduling and Alerting implemented)
**Constraint:** LLM/AI is the reasoning layer, not the core prediction engine.

---

## 1. Why Phase 9 Exists

Phases 7 (Availability) and 8 (Tactical) are engineering-complete but
**empirically blocked**: no historical archive of pre-deadline, unstructured
football intelligence exists that can pass
`InformationAccessPolicy.STRICT_REPRODUCIBILITY`. The only remedy is to start
accumulating that data **forward in time**, from now, with honest timestamps.

Phase 9 builds the **foundational architecture** for that accumulation and for
the AI-assisted synthesis layer that will eventually consume it. It does **not**
scrape live data, train ML models, modify Phases 1–6, evaluate the locked 2025/26
holdout, or assign A/B/C classifications.

---

## 2. Data Flow

```
Raw Text (press conference, tweet, article, transcript)
    │
    ▼
Temporal Ledger                    live_intelligence_raw_items
    │   • scraped_at, published_at (nullable), available_at, ingested_at
    │   • append-only; content-hash dedupe
    │   • deadline eligibility via classify_ledger_entry()
    ▼
LLM Extractor                     llm_extraction_runs + availability_evidence + tactical_evidence
    │   • sees ONLY the LedgerItemView (no outcomes, no DB state)
    │   • no timestamp fields in extraction envelope (extra="forbid")
    │   • grounding check: source_quote must be literal substring
    ▼
Structured Evidence               availability_evidence (Phase 7 table)
                                  tactical_evidence (Phase 9 table)
                                  live_availability_evidence_links (provenance)
    │
    ▼
AI Analyst                        AnalystReport (narrative synthesis)
    │   • reads DecisionPredictionProvider read-only
    │   • restates quantitative baseline verbatim
    │   • qualitative_adjustment = direction/magnitude only
    ▼
Decision-Facing Output            4 guardrails enforced post-hoc
```

---

## 3. Module Inventory

| Module | Responsibility |
|--------|----------------|
| `models.py` | ORM models: `LiveIntelligenceSource`, `LiveIntelligenceRawItem`, `LLMExtractionRun`, `TacticalEvidence`, `LiveAvailabilityEvidenceLink`. Enums: `LiveSourceType`, `CaptureMethod`, `LedgerTemporalClass`, `TacticalEvidenceType`, `TacticalDirection`, `ExtractionStatus`. |
| `temporal_ledger.py` | Single point where time enters the pipeline. `build_timestamps()` derives `available_at = max(published_at, scraped_at)` under `CONSERVATIVE` policy. `validate_timestamps()` enforces ordering invariants. `classify_ledger_entry()` decides `PRE_DEADLINE` / `POST_DEADLINE` / `NO_DEADLINE_CONTEXT`. `is_validation_evidence()` is the single predicate for empirical counting. `LedgerItemView` is the only thing the LLM extractor sees. |
| `ingestion.py` | `LiveIngestionPipeline`: register sources, ingest `RawTextSubmission`, dedupe by content hash, reject temporal violations, classify deadline eligibility. `IngestionReport` provides conservation checking. |
| `schemas.py` | Strictly-typed Pydantic contracts (`extra="forbid"`). `ExtractionEnvelope` maps to `availability_evidence` + `tactical_evidence`. `AnalystOutput` separates `quantitative_baseline` from `qualitative_adjustment`. No timestamp fields anywhere in LLM-facing schemas. |
| `prompts.py` | Versioned `PromptTemplate` / `LLMPrompt` objects. Three extraction templates: `AVAILABILITY`, `TACTICAL`, `COMBINED`. Shared rules block date-shifting and numeric prediction. Prompt hash persisted on every extraction run. |
| `extraction.py` | `PromptedLLMExtractor`: render → complete → parse JSON → Pydantic validate → grounding check → temporal inheritance. `persist_extraction()` writes to Phase 7 + Phase 9 tables. `usable_drafts()` filters to `PRE_DEADLINE` only. |
| `mock_llm.py` | `MockLLMProvider`: deterministic, rule-based, zero network. Keyword → evidence mapping. `is_mock=True` propagates to `LLMExtractionRun.is_mock`, excluding mock output from validation evidence. Scripted responses for adversarial testing. |
| `analyst_prompts.py` | Three analyst templates: `TRANSFER_RECOMMENDATION`, `CAPTAINCY_DEBATE`, `DIFFERENTIAL_RISK`. System prompt enforces restatement, no revised projections, citation-or-stay-silent. |
| `analyst.py` | `AIAnalyst`: reads `DecisionPredictionProvider` read-only, filters evidence pre-deadline, renders prompt, validates output, enforces 4 guardrails: restatement check, no invented baselines, citation validation, empty-evidence→neutral. |
| `llm_settings.py` *(9.1)* | Secure runtime config. `LLMSettings` holds API keys as `SecretStr`, prints redacted SHA-256 fingerprints only. `load_llm_settings()` reads a git-ignored `.env`; `LLMProviderName` / `model_for` / `has_api_key`. |
| `llm_providers.py` *(9.1)* | `RealLLMProvider` subclasses for Gemini, Groq, OpenRouter (`httpx`, no vendor SDK) behind the response cache, `max_tokens` cap, input guard, rate limiter and call budget. `ProviderFactory` builds providers wired to all safeguards. |
| `provider_router.py` *(9.1)* | `ProviderRouter` — task-based routing to the preferred provider, automatic fallback on rate-limit/auth errors, optional round-robin load balancing. Implements `LLMProvider`; never holds API keys, delegates to `ProviderFactory`. |
| `response_cache.py` *(9.1)* | Read-through cache keyed by `provider|model|prompt_hash|input_hash|max_output_tokens|temperature`. `SqliteResponseCache` persists to a git-ignored local file; cache hits cost zero quota. |
| `rate_limit.py` *(9.1)* | `RateLimiter` pacing plus `CallBudget` per-process ceiling; cache hits never consume the budget. |
| `prompt_registry.py` *(9.1)* | Prompt versioning: hashes template + schema version (`prompt_template_hash`), verifies the registry, fingerprints rendered prompts. |
| `scheduling/scheduler.py` *(9.6)* | `Scheduler` — orchestrates **fetch (Phase 9.5 connectors) → ingest (Phase 9.2) → alert → notify** per pass. Manual `run()` / scheduled `run_scheduled()`, `RateLimiter` pacing between passes, per-stage error isolation. Produces a `SchedulerRunReport`. |
| `scheduling/alerts.py` *(9.6)* | `AlertGenerator` — classifies `RawItem`s into `Alert` objects (injury / availability risk / tactical change / transfer news / general) with severity, via offline keyword heuristics. Rate-limited passes, per-item error isolation, `max_alerts_per_pass` flood cap. |
| `scheduling/notification.py` *(9.6)* | `NotificationService` + `Notifier` channels — `SlackNotifier` (HTTP webhook), `EmailNotifier` (SMTP), `LogNotifier`, `RecordingNotifier`. Rate-limited sends, per-channel error isolation, `NotificationDispatchReport` receipts. |
| `verification/live_verification.py` *(9.7)* | `RSSFeedVerifier` / `FPLAPIVerifier` — verify a live RSS feed / the official FPL API is reachable, parses, and is ingested into the Phase 9.2 pipeline. `EndToEndVerifier` — runs the full pipeline (fetch → ingest → extract → resolve → synthesize → report → alert → notify) over injected connectors and reports per-stage pass/fail. `build_verification_session` provides the shared in-memory SQLite verification DB. |

`ProviderRouter` routing defaults: **availability → Groq** (fast structured JSON), **tactical / combined → Gemini** (longer context), with fallback order Groq → Gemini → OpenRouter. Routing metadata (`provider_name`, `model_name`, `routing_strategy` = `task_based` / `fallback` / `round_robin`) is recorded on every `llm_extraction_runs` row and on `LLMResponse` / `ExtractionProvenance`, so the audit trail states *how* a provider was reached, not just *which* provider answered.

---

## 4. Non-Negotiables

### 4.1 Strict Separation

The quantitative engine (Phases 1–6) is never modified, re-fitted, or overwritten
by Phase 9. The AI Analyst consumes `PlayerPrediction` read-only and is
structurally incapable of emitting a revised point projection.

### 4.2 LLM is Reasoning, Not Prediction

The LLM converts unstructured text into typed evidence and writes prose. It
never produces a number that feeds the optimizer. `AnalystOutput` has no field
for a revised projection.

### 4.3 No Look-Ahead is Structurally Impossible

- The LLM never supplies a timestamp.
- Every temporal field on extracted evidence is inherited from the immutable
  ledger row.
- Deadline eligibility is decided by `temporal_ledger` using the existing
  `InformationAccessPolicy`.
- `LedgerItemView` contains no outcomes, no later rows, no database state.
- The extraction envelope has `extra="forbid"` and contains zero timestamp fields.

### 4.4 Mock is Never Evidence

Sources carry a `DataEnvironment` marker (`"real"` or `"mock"`). Mock-environment
ledger rows can never be reported as real validation evidence, regardless of
temporal class. `is_validation_evidence()` requires `PRE_DEADLINE + real +
not-mock-extraction`.

### 4.5 Nothing is Silently Dropped

Unresolved players, ungrounded quotes, schema-rejected payloads, and
provider errors are recorded with a reason on `LLMExtractionRun` or in
`PersistenceReport.unresolved`.

---

## 5. Temporal Contract

Four mandatory fields on every `LiveIntelligenceRawItem`:

| Field | Nullable | Source | Enforcement |
|-------|----------|--------|-------------|
| `published_at` | Yes (never fabricated) | Source metadata | `publication_established` flag |
| `scraped_at` | No | Capture step | Pipeline clock |
| `available_at` | No | Derived | `max(published_at, scraped_at)` under `CONSERVATIVE` |
| `ingested_at` | No | Pipeline clock | Caller cannot supply it |

Ordering invariants (validated by `validate_timestamps()`):
- `published_at <= scraped_at`
- `scraped_at <= ingested_at`
- `published_at <= available_at`
- `available_at <= ingested_at`
- No timestamp is in the future relative to the injected clock

`event_time` is deliberately *not* ordered against the others: a press conference
published today can legitimately describe a future absence.

---

## 6. Database Schema

### 6.1 Phase 9 Tables

| Table | Purpose |
|-------|---------|
| `live_intelligence_sources` | Source registry (type, reliability, capture method, environment) |
| `live_intelligence_raw_items` | Append-only temporal ledger (content hash dedupe) |
| `llm_extraction_runs` | Provenance for every extraction call (provider, model, prompt hash, mock flag) |
| `tactical_evidence` | Phase 8-style tactical signals with inherited temporal fields |
| `live_availability_evidence_links` | Provenance bridge from Phase 7 `availability_evidence` to ledger |

### 6.2 Phase 7 Tables (Unmodified)

Availability evidence extracted by the LLM is written to the existing
`availability_evidence` table. Phase 9 does not fork or alter Phase 7 schemas.

---

## 7. Extraction Contract

### 7.1 What the LLM May Emit

- Categorical evidence types from Phase 7 (`EvidenceType`, `AvailabilityStatus`)
  and Phase 8 (`TacticalEvidenceType`) taxonomies.
- Self-assessed confidence (0.0–1.0).
- Verbatim `source_quote` (must be a literal substring of the ledger text).
- Reasoning text.

### 7.2 What the LLM May Not Emit

- **Any timestamp.** No temporal fields exist in any extraction model.
- **Any point projection, expected minutes, price, or optimiser input.**
- **Any unknown key.** All models use `extra="forbid"`.

### 7.3 Grounding

`quote_is_grounded(quote, raw_text)` performs whitespace-normalised,
case-folded substring check. A paraphrase is a hallucination and is rejected.

---

## 8. Analyst Contract

### 8.1 Input

- `QuantitativeBaseline` (read-only snapshot of `PlayerPrediction`).
- `EvidenceCitation[]` (pre-deadline, real, non-mock evidence only).

### 8.2 Output Shape

```json
{
  "schema_version": "phase9.analyst.v1",
  "task": "transfer_recommendation",
  "headline": "...",
  "quantitative_baseline": [
    {
      "subject_ref": "player:1",
      "expected_points": 5.5,
      "start_probability": 0.8,
      "floor": 2.0,
      "ceiling": 10.0,
      "interpretation": "..."
    }
  ],
  "qualitative_adjustment": {
    "direction": "up|down|neutral",
    "magnitude": "none|low|moderate|high",
    "cited_evidence_refs": ["ev_1"],
    "rationale": "..."
  },
  "net_assessment": "...",
  "recommendation": "proceed|hold|monitor|avoid|no_recommendation",
  "confidence": 0.6,
  "caveats": ["..."]
}
```

### 8.3 Four Guardrails

1. **Restatement check:** Every supplied subject must be restated with identical numbers (`RESTATEMENT_TOLERANCE = 1e-4`).
2. **No invented baselines:** Analyst cannot cite subjects it was not given.
3. **Citation validation:** Every `cited_evidence_refs` entry must exist in the supplied evidence bundle.
4. **Empty evidence → neutral:** No evidence means direction=`neutral`, magnitude=`none`, empty refs.

---

## 9. Mock Provider

`MockLLMProvider` is a test double, not a simulation of a language model.

- Deterministic: same prompt → same output.
- Rule-based: keyword → evidence mapping (availability + tactical).
- `is_mock=True` propagates to `LLMExtractionRun.is_mock`.
- Scripted responses override rule generation for adversarial testing.
- Zero network, zero RNG, zero environment lookups.

---

## 10. Testing Strategy

All Phase 9 tests use `sqlite:///:memory:` with `Base.metadata.create_all()`.
The mock provider ensures zero API calls and deterministic outputs.

**Coverage:**
- `test_phase9.py`: 49 tests covering:
  - Temporal ledger: derivation, validation, classification, content hashing.
  - Ingestion: source registration, deduplication, temporal rejection, deadline classification, report conservation.
  - Extraction: schema validation, grounding rejection, malformed JSON, extra-key rejection, pre-deadline filtering, mock provider output.
  - Persistence: run creation, evidence persistence, unresolved player recording.
  - Analyst guardrails: restatement verification, empty evidence, missing citations, mock evidence exclusion, post-deadline leakage rejection.
  - Quote grounding: exact, whitespace, case, missing, empty.
  - Prompt templates: schema version rendering, hash uniqueness.

---

## 11. Worked Example

```python
from datetime import UTC, datetime
from fpl_intelligence.live_intelligence.ingestion import LiveIngestionPipeline, RawTextSubmission
from fpl_intelligence.live_intelligence.extraction import PromptedLLMExtractor
from fpl_intelligence.live_intelligence.mock_llm import make_mock_provider
from fpl_intelligence.live_intelligence.temporal_ledger import TemporalLedger

# 1. Register source
pipeline = LiveIngestionPipeline(db, default_environment=DataEnvironment.REAL)
pipeline.register_source(
    "club_official_twitter",
    source_type=LiveSourceType.SOCIAL_POST,
    environment=DataEnvironment.REAL,
    publication_timestamp_trusted=True,
)

# 2. Ingest raw text
submission = RawTextSubmission(
    source_name="club_official_twitter",
    raw_text="Mohamed Salah is ruled out with a hamstring injury.",
    scraped_at=datetime(2025, 8, 15, 10, 0, 0, tzinfo=UTC),
    published_at=datetime(2025, 8, 15, 9, 30, 0, tzinfo=UTC),
)
outcome = pipeline.ingest(submission)
assert outcome.status is IngestionStatus.CREATED

# 3. Extract evidence
provider = make_mock_provider(player_names=["Mohamed Salah"])
extractor = PromptedLLMExtractor(provider)
ledger = TemporalLedger(db)
item = db.get(LiveIntelligenceRawItem, outcome.raw_item_id)
view = ledger.to_view(item)
result = extractor.extract(view)
assert result.status is ExtractionStatus.OK

# 4. Persist
report = persist_extraction(db, result, season_id=2025)
assert report.availability_persisted == 1
assert report.extraction_run.is_mock is True  # excluded from validation evidence
```

---

## 12. Constraints Honoured

| Constraint | Status |
|------------|--------|
| LLM/AI is reasoning layer, not prediction engine | Enforced by schema + guardrails |
| Strict separation Phases 1–6 vs Phase 9 | No Phase 1–6 code modified |
| No look-ahead leakage | Structurally impossible (see §4.3) |
| No model training | No training code exists |
| No Phase 4/5/6 modification | Zero changes to those modules |
| No 2025/26 holdout evaluation | Not touched |
| No live scraping | Ingestion is text-in only |
| No A/B/C classification | Not assigned |
| Mock LLM for tests | `MockLLMProvider` used everywhere; `is_mock=True` |
| Temporal fields enforced | `published_at`, `scraped_at`, `available_at`, `ingested_at` on every ledger row |
| LLM outputs strictly typed JSON | `extra="forbid"` on all Pydantic models |
| PostgreSQL enum reconciled with Python enum | Migration `0011_phase912_availability_enum` adds `available` to native `availabilitystatus` enum |

---

## 13. Remaining Tasks

1. Wire a real live-scraping source (manual paste is the current input path).
2. Implement entity resolution hook (`resolve_player`, `resolve_team`) that
   matches the canonical Phase 7 provider key.
3. Run empirical backtest once a `STRICT_BACKTEST_SAFE` historical source is
   acquired or live data accumulates past a deadline.
4. Do **not** assign A/B/C until empirical validation is possible.

---

## 14. Phase 9.1.1 — Availability Vocabulary Reconciliation

### 14.1 Canonical status list (as of Phase 9.1.1)

`AvailabilityStatus` (the single canonical vocabulary shared by Phase 7 and
Phase 9) is now:

```
start, bench, available, doubtful, questionable, suspect, out, suspended, unknown
```

`available` was added because the live dry-run against Groq legitimately
returned `status_mentioned = "available"` for a player who "trained fully and
is available, but will he start?" Forcing the model to collapse that into
`start` or `unknown` would be semantically wrong, so the vocabulary — not the
model — was reconciled. **Any source term is normalised onto these nine values;
the extraction schema accepts no others** (`flying` → schema rejection).

### 14.2 Phase 7 state mapping (heuristic, pending calibration)

`available` means *fit / available / in contention, but not confirmed to start*.
It was added to the Phase 7 status-handling tables with a conservative mapping:

| Map | Value | Meaning |
|-----|-------|---------|
| `evidence._STATUS_ORDER` | between `START` and `BENCH` | severer than `start`, lest `OUT`/`DOUBTFUL` lose to it |
| `state._STATUS_START_PROB` | `0.80` | interpolated between `START` (0.95) and `BENCH` (0.75) |
| `state._STATUS_MINUTES_FACTOR` | `0.85` | interpolated between `START` (1.0) and `BENCH` (0.15) |

> **Important:** the two numeric mappings for `available` are **live-engineering
> heuristics, not empirically validated constants.** They have no historical
> calibration yet (Phase 9 data does not exist to fit them). They MUST be
> replaced by empirically calibrated values once live evidence can be backtested.
> The code and tests mark them as such and do not claim empirical support.

`historical/event_types.py` now maps `HistoricalEventType.AVAILABLE` →
`AvailabilityStatus.AVAILABLE` (was `START`).

### 14.3 Prompt template (v1.1.0)

The shared extraction rules now:
- enumerate the full canonical status list in the `status_mentioned` field;
- give worked normalisation examples (`ruled out → out`, `suspended →
  suspended`, `touch and go → doubtful`, `trained fully and available →
  available`, `expected to start → start`, `back in training but not this
  weekend → available or bench depending on wording`);
- instruct the model to use `unknown` **only** when no status can be inferred.

The template version was bumped to `1.1.0` for all three extraction templates,
and `PROMPT_HASH_LOCK` was updated (see `prompt_registry.py`).

### 14.4 Dry-run / free-tier changes

- **`max_output_tokens` raised 1024 → 2048** (both the `FREE_TIER_*` constant
  and the `LLMSettings` default). Provider-specific caps still bound the worst
  case per request.
- **Truncation warning** — the dry-run prints a warning if
  `completion_tokens >= max_output_tokens`.
- **Free-tier accounting** — the dry-run previously reported `live API calls
  made = 0` under the router because `ProviderRouter` did not expose
  `live_calls`. The router now folds every delegated provider's `live_calls`
  (task-based call, retries, and fallback calls) into a single counter, so
  routed runs report their true usage. Cache hits still cost 0.
  - **Fallback diagnostics** — when `routing_strategy = fallback`, the dry-run
    prints the primary provider attempted and a coarse failure reason
    (`rate_limit`, `auth`, `timeout`, `schema_error` or `other`). No secrets are
    printed — only provider names and reason categories.

### 14.5 Code-review remediation (findings 1–7)

A `/review uncommitted` pass after closure flagged 10 issues in the Phase 9.1
layer; findings 1–7 were fixed in source (findings 8–10 deferred as tech debt).
The shared safeguards behaviour is the most consequential:

- **One budget / limiter / cache per router (was finding 1).** `ProviderRouter`
  now owns a single `CallBudget`, `RateLimiter` and `ResponseCache` and passes
  them to `ProviderFactory.create()` for *every* built provider — primary,
  retry and fallback. Previously each build got fresh per-call instances, so the
  `LLM_MAX_CALLS_PER_RUN` ceiling was never enforced across routed calls and
  pacing history reset on every call. The router exposes the shared objects as
  `router.budget` / `router.rate_limiter` / `router.cache`.
- **Budget charged per HTTP request (was finding 5).** `RealLLMProvider` no
  longer pre-charges on `complete()`; `_consume_request_slot()` claims one slot
  per real attempt inside `_invoke_with_retry()`, so retries count honestly.
  `live_calls` aliases `live_requests` (every request, not just successes).
- **Backoff floors at the pacing interval (was finding 7).** `_retry_after_seconds`
  treats non-positive / invalid `Retry-After` as absent (no zero-delay retry);
  `_backoff` floors the delay at `RateLimiter.min_interval_seconds` and caps at
  60s.
- **Mock keyword matching is word-bounded (was finding 2).** `"available"` no
  longer matches inside `"unavailable"`; an explicit `"unavailable" → DOUBTFUL`
  rule was added.
- **Corroboration ranks `START` above `AVAILABLE` (was finding 3)** — an explicit
  start beats a vague "available".
- **`LLM_MODEL` override is primary-only (was finding 4)** — it no longer leaks
  onto fallback providers (`model_for` applies it only when
  `target == settings.llm_provider`).
- **Historical FPL `"a"` code → `AVAILABLE` (was finding 6)** — aligned
  `_map_fpl_status` with the event-type mapping (`START` was a split-brain).

Regression coverage lives in `tests/unit/test_phase9_finding_remediations.py`.

---

## 15. Phase 9.1.2 — PostgreSQL Enum Migration

### 15.1 Problem

Phase 9.1.1 added `AVAILABLE = "available"` to the Python
`AvailabilityStatus` enum in `models.py` and updated all SQLAlchemy/alembic
migrations that define the enum *from metadata* (e.g. `sa.Enum(AvailabilityStatus, ...)`).
However, the **native PostgreSQL enum type** `availabilitystatus` — created by
Alembic migration `0006_phase7_availability` — was never altered to include the
`available` value.

On PostgreSQL, a native enum type cannot store a value it does not know about.
Any attempt to persist an `availability_evidence` or `availability_events`
row with `status_mentioned = 'available'` on a pre-9.1.2 database would raise:

```
psycopg2.errors.InvalidTextRepresentation: enum value "available" is not valid
```

This was the final blocker for the live extraction path, even though the dry-run
against the mock provider and the SQLite-backed unit tests all passed (SQLite
does not enforce native enum membership; its `SAEnum` stores values as `TEXT`
with a `CHECK` constraint derived from the model metadata, which was already
updated).

### 15.2 Solution

**Migration file:** `migrations/versions/0011_phase912_availability_enum.py`

The migration runs `ALTER TYPE availabilitystatus ADD VALUE IF NOT EXISTS
'available' AFTER 'start'` inside an Alembic `autocommit_block`, because
PostgreSQL does not allow `ALTER TYPE ... ADD VALUE` inside an explicit
transaction. Key properties:

| Property | Behaviour |
|----------|-----------|
| **Idempotent** | `IF NOT EXISTS` guard + pre-check via `_enum_values()` query against `pg_enum` |
| **Guarded** | Only runs on `postgresql://` dialect; no-op on SQLite/MySQL |
| **Ordered** | `AFTER 'start'` places it between `start` (0.95 start probability) and `bench` (0.75), matching `_STATUS_ORDER` |
| **Downgrade** | Documented no-op — PostgreSQL cannot safely drop an enum value that may be referenced by existing rows. The downgrade function is a no-op by design; dropping requires a manual `DROP TYPE` + `CREATE TYPE` + column rewrite after data audit |
| **Dry-run verified** | `alembic upgrade head` against SQLite passes; all 141 unit tests pass |

### 15.3 Deployment

```bash
alembic upgrade head
```

After running, the PostgreSQL `availabilitystatus` enum will contain all nine
values: `start, bench, available, doubtful, questionable, suspect, out,
suspended, unknown`.

---

## 16. Phase 9.3 — Intelligence Report Synthesis Layer

### 16.1 Overview

Phase 9.3 adds the presentation layer that transforms a `PlayerPrediction` +
extracted `EvidenceCitation[]` into a human-readable `IntelligenceReport`. It is
the "what should I do with this intelligence?" step — purely narrative and
advisory, never feeding numbers back into the quantitative optimizer.

### 16.2 New / Extended Modules

| Module | Responsibility |
|--------|----------------|
| `report.py` (new) | `PredictionContext`, `IntelligenceReport`, `ReportConfidence`, `ReportEvidenceCitation`, `ReportQuantitativeBaseline`, `ReportQualitativeAdjustment`, `UnresolvedWarning`. Includes `render_markdown()`. |
| `analyst.py` (extended) | `AIAnalyst.generate_report()`, `captaincy_report()`, `differential_report()` — delegate to `analyse()` then translate into `IntelligenceReport`. |
| `scripts/manual_ingest_raw_text.py` (extended) | `--analyst` flag runs synthesis after ingestion and prints the report as Markdown. |

### 16.3 IntelligenceReport Shape

```json
{
  "schema_version": "phase9.report.v1",
  "task": "transfer_recommendation",
  "headline": "Transfer Recommendation: Player 1",
  "prediction_context": { "subject_ref": "player:1", "player_id": 1, "gameweek": 1, ... },
  "qualitative_adjustment": { "direction": "neutral", "magnitude": "low", "cited_evidence_refs": [], "rationale": "..." },
  "net_assessment": "...",
  "recommendation": "hold",
  "confidence": 0.6,
  "confidence_band": "low",
  "citations": [ { "evidence_ref": "ev_1", "kind": "availability", ... } ],
  "unresolved_warnings": [ { "evidence_ref": "ev_1", "kind": "excluded", ... } ],
  "caveats": ["..."],
  "generated_at": "2026-08-13T...",
  "provider_name": "mock",
  "model_name": "scripted",
  "is_mock": true,
  "prompt_hash": "..."
}
```

### 16.4 Three Analyst Tasks

1. **Transfer Recommendation** (`AnalystTask.TRANSFER_RECOMMENDATION`) —
   buy/hold/sell guidance based on injury/tactical news relative to the
   quantitative baseline.
2. **Captaincy Debate** (`AnalystTask.CAPTAINCY_DEBATE`) — compares multiple
   players' baselines side-by-side and argues for/against each as captain.
3. **Differential Risk** (`AnalystTask.DIFFERENTIAL_RISK`) — evaluates a less-owned
   player against higher-owned alternatives, weighing information asymmetry.

### 16.5 Constraints Honoured

- `IntelligenceReport` is a **presentation layer** only; it never feeds back into
  `DecisionPredictionProvider` or any optimizer.
- `PredictionContext` carries quantitative numbers read-only; the analyst cannot
  revise them (structural guarantee — it has no output field for a new projection).
- `MockLLMProvider` is used in all unit tests; no live API calls in `pytest`.
- `is_mock` and `provider_name`/`model_name` are recorded on every report for
  auditability.

- `is_mock` and `provider_name`/`model_name` are recorded on every report for
  auditability.

---

## 17. Phase 9.4 — Quantitative Bridge and Evidence Query Layer

### 17.1 Overview

Phase 9.3 left the AI Analyst able to synthesise evidence and predictions, but
it still required the `PredictionContext` to be assembled by hand (via CLI
flags on `scripts/manual_ingest_raw_text.py`). Phase 9.4 removes that manual
step by connecting the Analyst to the **real quantitative engine** (Phases
4/5/6) and the **live evidence database**, so reports are generated
automatically from real predictions and stored pre-deadline evidence.

Three components (all in `src/fpl_intelligence/live_intelligence/bridge.py`):

1. **`PredictionContextBuilder`** — the *quantitative bridge*. Reads a
   `PlayerPrediction` from a `DecisionPredictionProvider` (read-only) and
   returns a populated :class:`PredictionContext`. The quantitative engine is
   never modified — only consumed through its existing interface.
2. **`EvidenceQueryService`** — the *evidence query layer*. Queries resolved
   `AvailabilityEvidence` (Phase 7), resolved `TacticalEvidence` (Phase 8), and
   `UnresolvedLiveEvidence` (Phase 9.2.1), filters each by the gameweek cutoff
   under the `InformationAccessPolicy`, excludes mock-environment evidence by
   default, and returns an `EvidenceQueryResult` of `EvidenceCitation` objects.
3. **`AnalystReportGenerator`** — the *orchestrator*. Builds the
   `PredictionContext` via (1), queries evidence via (2), and delegates to
   `AIAnalyst.generate_report()` to produce the `IntelligenceReport`. With no
   evidence it returns the analyst's neutral report.

A `StaticPredictionProvider` (a trivial `DecisionPredictionProvider`) is shipped
for `--dry-run` and unit tests; it never touches an API or database.

### 17.2 Data Flow

```
player_id + gameweek + cutoff
        │
        ▼
PredictionContextBuilder ──DecisionPredictionProvider──► PredictionContext (read-only)
        │
EvidenceQueryService          availability_evidence (Phase 7, active, pre-cutoff)
        │                      tactical_evidence (Phase 8, active, pre-cutoff)
        │                      unresolved_live_evidence (Phase 9.2.1, pre-cutoff)
        │                      ── filtered by InformationAccessPolicy, mock excluded
        ▼
EvidenceQueryResult (EvidenceCitation[])
        │
        ▼
AnalystReportGenerator ──AIAnalyst.generate_report()──► IntelligenceReport (Markdown)
```

### 17.3 Evidence Filtering Rules

- **Cutoff / no look-ahead.** Each citation's `available_at` (and `ingested_at`
  under `STRICT_REPRODUCIBILITY`) must be `<= cutoff`. The same
  `InformationAccessPolicy` used elsewhere in the engine drives
  `_temporal_condition()`, so look-ahead is structurally impossible.
- **Mock is never evidence.** Sources carry a `DataEnvironment` marker. By
  default `EvidenceQueryService` drops evidence whose source is `mock`;
  `allow_mock=True` opts in for test/dry-run paths.
- **Phase 7 historical rows without a ledger link** are returned with
  `source_name="phase7_historical"`; their `temporal_class` is
  `no_deadline_context`, so the Analyst will never cite them (they carry no
  resolved deadline context).
- **Inactive evidence excluded** — `is_active = True` is required for both
  availability and tactical evidence.
- **Unresolved evidence is not player-scoped** — a row whose entity could not be
  resolved is returned for any player query and surfaced as an analyst
  `unresolved_warning`.

### 17.4 CLI

`scripts/generate_intelligence_report.py`:

```bash
# Dry run (offline; MockLLMProvider + StaticPredictionProvider, no DB writes)
python scripts/generate_intelligence_report.py --player-id 1 --gameweek 3 --dry-run

# Against a real DB with mock LLM (no network) and real saved predictions
python scripts/generate_intelligence_report.py --player-id 1 --gameweek 3 --db ./fpl.db

# Live LLM (requires .env) with explicit cutoff and a different task
python scripts/generate_intelligence_report.py --player-id 7 --gameweek 4 \
    --cutoff 2025-08-17T18:30:00+00:00 --task captaincy_debate --db ./fpl.db --provider real
```

`--cutoff` is an ISO-8601 string and defaults to the current UTC time.
`--dry-run` uses `MockLLMProvider` + `StaticPredictionProvider` and never issues
DB writes. `--provider real` builds a guarded real provider via
`ProviderFactory` (credentials from the git-ignored `.env` only — never
hardcoded).

### 17.5 Testing & Quality

- `tests/unit/test_phase9_4_bridge.py` — 38 tests covering
  `PredictionContextBuilder` (mocked `DecisionPredictionProvider`),
  `EvidenceQueryResult`, `EvidenceQueryService` (availability/tactical/
  unresolved, cutoff filtering, mock exclusion, player scope, inactive
  exclusion), `AnalystReportGenerator` (end-to-end with `MockLLMProvider`,
  neutral-no-evidence, mock-excluded), and `StaticPredictionProvider`.
- **Full suite: 522 passed** (previously 484).
- `ruff` and `mypy` clean on `bridge.py` and the new CLI script.
- **No migration required.** Phase 9.4 introduces no new tables, columns, or
  enums — it reads existing Phase 4/5/6 prediction interfaces and the existing
  Phase 7/8/9.2.1 evidence tables.
- **No live API calls in `pytest`.** All tests use `MockLLMProvider` /
  `StaticPredictionProvider` against an in-memory SQLite database.
---
## 18. Phase 9.5 — Live Source Connectors

### 18.1 Overview

Phase 9.4 left the Analyst able to turn *stored* evidence into reports, but
every raw item still had to be captured and pasted by hand. Phase 9.5
automates the ingestion of news by introducing a small **Live Source
Connector** layer: each connector fetches raw items from one live source, and a
**ConnectorScheduler** orchestrates the available connectors on demand or on a
schedule, handing the fetched items straight to the Phase 9.2 ingestion
pipeline.

This layer is **additive and offline-testable**:

- it does **not** modify the quantitative Phases 1–8 stack,
- it makes **no live API calls inside `pytest`** (tests inject
  `httpx.MockTransport` clients),
- it **hardcodes no API keys** (the official FPL `bootstrap-static` endpoint
  requires none; readers that later need keys read them from the injected
  `headers` / env only),
- and it performs **no aggressive scraping** — it only polls declared RSS
  feeds and the official API at a rate-limited, polite interval.

### 18.2 Components

All code lives in `src/fpl_intelligence/live_intelligence/connectors/`:

| Module | Component | Responsibility |
|--------|-----------|----------------|
| `base.py` | `SourceConnector` (ABC) | The interface every fetcher implements: `fetch() -> list[RawItem]`. Shared HTTP plumbing (`_get`), rate limiting via the Phase 9.1 `RateLimiter`, typed exceptions (`SourceConnectorError` / `SourceConnectionError` / `SourceParseError`), and `_build_raw_item()` which constructs a Phase 9.2 `RawItem`. |
| `rss.py` | `RSSConnector` | Fetches an RSS 2.0 feed, parses `<item>`s with the stdlib `xml.etree`, and extracts title, content (description / content:encoded), `published_at` (RFC 822 or ISO-8601, namespace-agnostic), and the permalink / GUID. |
| `fpl_api.py` | `FPLAPIConnector` | Fetches the official FPL `bootstrap-static` JSON and projects each player into a `RawItem`: any non-empty `news`, or a `chance_of_playing_* < 100` availability risk. Player FPL id → `external_id`; fetch time → `published_at`. |
| `scheduler.py` | `ConnectorScheduler` | Runs the connectors, forwards each `RawItem` to an `IngestionSink`, and isolates failures per connector / per item. Supports manual triggering (`run()`) and scheduled execution (`run_scheduled()`). Produces a `SchedulerReport`. |

**The connector↔pipeline contract.** A connector returns
`list[RawItem]` — the very model the Phase 9.2 `ingest_raw_text` already
consumes — so the scheduler's sink is a one-line call
(`ingest_raw_text(db, source_id=..., text=..., published_at=..., ...)`). No type
conversion, no loss of provenance: `source_id`, `url`, `external_id`,
`published_at`, `available_at` and `content_hash` all ride along.

**Time handling.** `scraped_at` / `ingested_at` come from the connector's
injected clock; `available_at` defaults to `published_at` (we never claim access
before publication). RSS items whose `published_at` is in the future (source
clock skew) are dropped rather than fabricated; items without a parseable date
default to the fetch time, which is the honest minimum.

### 18.3 Data Flow

```
RSS feed / FPL bootstrap-static  (network)
        │
        ▼
Connector.fetch()  ──►  list[RawItem]   (rate-limited, typed errors)
        │
        ▼
ConnectorScheduler.run() / run_scheduled()
        │  per connector, error-isolated
        ▼
IngestionSink ── ingest_raw_text ──► Phase 9.2 pipeline (dedupe, ledger, extraction)
        │
        ▼
SchedulerReport (fetched / ingested / errors)
```
### 18.4 CLI

`scripts/run_live_ingestion.py`:

```bash
# All connectors, one pass (persists into an in-memory DB)
python scripts/run_live_ingestion.py --connector all

# RSS only, custom feed
python scripts/run_live_ingestion.py --connector rss --rss-url https://...

# Official FPL API only
python scripts/run_live_ingestion.py --connector fpl_api

# Fetch but do not persist (safe to inspect)
python scripts/run_live_ingestion.py --connector all --dry-run

# Scheduled execution against a real DB file
python scripts/run_live_ingestion.py --connector all --interval 60 --iterations 5 --db ./fpl.db
```

`--connector` selects `rss`, `fpl_api`, or `all`. `--dry-run` still *fetches*
from the live sources but the ingestion pipeline rolls back so nothing is
persisted. `--interval` / `--iterations` enable and bound scheduled execution.
Without `--db` an in-memory SQLite database is used, so a bare run never
touches a real database.

### 18.5 Testing & Quality

- `tests/unit/test_phase9_5_connectors.py` — 35 tests: `RSSConnector` (RSS
  parsing incl. namespaces + ISO/RFC822 dates, empty feed, skipped title-only
  items, future-date drop, HTTP 500/429/`ConnectError`, invalid XML, `limit`,
  rate-limit pacing, custom `source_id`), `FPLAPIConnector` (news + chance
  extraction, fully-available player skip, string chance values, invalid JSON /
  missing elements / HTTP error, `limit`), and `ConnectorScheduler`
  (fetch→sink for all/single connector, per-connector fetch-error isolation,
  per-item sink-error isolation, scheduled execution count + dry-run flag,
  report totals, unknown connector). All HTTP is mocked with
  `httpx.MockTransport` — **zero live network calls in `pytest`**.
- **Full suite: 557 passed** (previously 522; Phase 9.5 adds 35 tests).
- `ruff` and `mypy` clean on the `connectors/` package, the test module, and
  the new CLI script.
- **No migration required.** Phase 9.5 introduces no new tables, columns, or
  enums — it consumes the existing Phase 9.2 `ingest_raw_text` pipeline.

---
## 19. Phase 9.6 — Scheduling and Alerting

### 19.1 Overview

Phase 9.5 gave the engine connectors that fetch live news on demand; Phase 9.6
closes the automation loop:

* a **Scheduler** runs the full pipeline — fetch → ingest → alert → notify —
  on demand or on a schedule,
* an **AlertGenerator** turns freshly-ingested raw items into user-facing
  alerts (injury news, availability risk, tactical changes, transfer news,
  general), and
* a **NotificationService** fans those alerts out to the user through one or
  more channels (Slack webhook, SMTP email, local log).

The layer is **additive and offline-testable**, honouring the same constraints
as Phase 9.5:

* it does **not** modify the quantitative Phases 1–8 stack — the `Scheduler`
  only *wraps* the existing Phase 9.5 `ConnectorScheduler` and hands items to
  the existing Phase 9.2 `ingest_raw_text` pipeline;
* it makes **no live API calls inside `pytest`** — connector HTTP is mocked
  with `httpx.MockTransport`, Slack webhook HTTP is mocked the same way, and
  Email SMTP is an injected seam;
* it **hardcodes no API keys** — the Slack webhook URL and SMTP credentials
  come from CLI arguments or environment variables only;
* and it performs **no aggressive scraping** — the scheduler only re-uses the
  rate-limited RSS / official-API connectors from Phase 9.5.

### 19.2 Components

All code lives in `src/fpl_intelligence/live_intelligence/scheduling/`:

| Module | Component | Responsibility |
|--------|-----------|----------------|
| `scheduler.py` | `Scheduler` | Wraps a Phase 9.5 `ConnectorScheduler` and runs one pipeline pass per call: `run()` (manual) or `run_scheduled()` (loop with `interval_seconds`, bounded by `iterations` or a `stop_event`). The ingestion sink is injected `(raw, *, connector, dry_run)`; the CLI wires it to Phase 9.2 `ingest_raw_text`. Alert and notify stages are optional; failures in any stage are captured on the `SchedulerRunReport` and never abort the pass. A `RateLimiter` paces successive passes. |
| `alerts.py` | `AlertGenerator` | Classifies `RawItem`s with case-insensitive keyword matching (one alert per item, highest-priority type) and emits `Alert` objects with a `severity` and provenance (`source_id`, `url`, `external_id`, `raw_item_id`). Each `generate()` pass acquires a `RateLimiter`; per-item errors are recorded on the `AlertGenerationReport`; `max_alerts_per_pass` caps the batch. |
| `notification.py` | `NotificationService` + `Notifier`s | Fans `Alert`s out to every configured channel: `SlackNotifier` (POSTs to an incoming webhook over `httpx`), `EmailNotifier` (stdlib `smtplib`; injectable SMTP seam), `LogNotifier` (dry-run-safe local sink), `RecordingNotifier` (in-memory capture for tests). Every send is rate-limited; one failing channel becomes a failed `NotificationReceipt` and never aborts the other channels. |

**The alert taxonomy.** `AlertType` ∈ {`injury`, `availability_risk`,
`tactical_change`, `transfer_news`, `general`}, with default severities
{injury: high, availability_risk: medium, tactical_change: medium,
transfer_news: low, general: low}. Classification is deliberately *local and
heuristic* (no LLM, no network), so alert generation cannot be blocked on
quota or a provider. A future LLM-backed classifier can slot into the same
`classifier` seam and inherits the existing rate limiting.

**The notify contract.** A `Notifier.send(notification)` either returns an ok
`NotificationReceipt` or raises `NotificationError`. The `NotificationService`
converts a raised error into a failed receipt, so a broken channel can never
propagate into the scheduler. `NotificationDispatchReport` aggregates
`alerts` / `attempted` / `delivered` / `failed` and per-channel receipts.

### 19.3 Data Flow

```
RSS feed / FPL bootstrap-static                (network)
        │
        ▼
Connector.fetch() ──► list[RawItem]            (Phase 9.5, rate-limited, typed errors)
        │
        ▼
Scheduler.run() / run_scheduled()              (Phase 9.6, RateLimiter-paced)
        │  per connector, error-isolated
        ▼
IngestionSink ── ingest_raw_text ──► Phase 9.2 pipeline (dedupe, ledger, extraction)
        │
        ▼
AlertGenerator.generate(ingested_items) ──► list[Alert]   (offline keyword classification)
        │
        ▼
NotificationService.send_alerts(alerts) ──► NotificationDispatchReport
        │  per channel (slack / email / log), rate-limited, error-isolated
        ▼
SchedulerRunReport (connector_report + alerts + notifications + errors)
```

### 19.4 CLI

`scripts/run_scheduler.py`:

```bash
# All connectors, one pass (persists into an in-memory DB, alerts to the log)
python scripts/run_scheduler.py --connector all

# RSS only, custom feed, fetch but do not persist
python scripts/run_scheduler.py --connector rss --rss-url https://... --dry-run

# Official FPL API only, no alerts, no notifications
python scripts/run_scheduler.py --connector fpl_api --no-alerts --notify none

# Alert to a Slack webhook (URL from --slack-webhook-url or SLACK_WEBHOOK_URL)
python scripts/run_scheduler.py --connector all --notify slack \
    --slack-webhook-url https://hooks.slack.com/...

# Alert by email (SMTP credentials from args or SMTP_USERNAME / SMTP_PASSWORD env)
python scripts/run_scheduler.py --connector all --notify email \
    --email-from alerts@example.com --email-to me@example.com \
    --smtp-host smtp.example.com --smtp-port 587 --smtp-user me --smtp-password "***"

# Scheduled execution against a real DB file
python scripts/run_scheduler.py --connector all --interval 60 --iterations 5 --db ./fpl.db
```

`--connector` selects `rss`, `fpl_api`, or `all`. `--dry-run` still *fetches*
from the live sources but the Phase 9.2 pipeline rolls back so nothing is
persisted (`--notify log` still prints the generated alerts). `--interval` /
`--iterations` enable and bound scheduled execution. Without `--db` an
in-memory SQLite database is used, so a bare run never touches a real database.
Exit codes: `0` success, `1` usage/configuration error, `2` provider/network
error.

### 19.5 Testing & Quality

- `tests/unit/test_phase9_6_scheduling_alerting.py` — **43 tests** covering
  `Scheduler` (fetch→ingest→alert→notify for RSS + FPL connectors with
  `httpx.MockTransport`, single-connector selection, per-connector fetch-error
  and per-item ingest-error isolation, dry-run forwarding, scheduled execution
  + stop-event, rate-limit pacing, alert/notify stage failure isolation),
  `AlertGenerator` (all five alert types, no-match, limit + flood cap,
  per-item classifier-error isolation, rate-limit pacing, `to_dict`), and
  `NotificationService` (Slack webhook success / HTTP 500 / `ConnectError`,
  Email `sendmail` success / failure, log + recording sinks, per-channel error
  isolation, batch totals, rate-limit pacing, batch cap). **Zero live network
  calls in `pytest`.**
- **Full suite: 594 passed** (previously 551; Phase 9.6 adds 43 tests).
- `ruff` and `mypy` clean on the `scheduling/` package, the test module, and
  the new CLI script.
- **No migration required.** Phase 9.6 introduces no new tables, columns, or
  enums — it consumes the existing Phase 9.5 `ConnectorScheduler` and the
  Phase 9.2 `ingest_raw_text` pipeline.

---

## 20. Live End-to-End Verification (Phase 9.7)

### 20.1 Purpose

Phase 9.7 verifies that the whole live ingestion chain works **end-to-end with
real data**: an RSS feed and the official FPL API are fetched live, the news is
ingested into the Phase 9.2 pipeline, the LLM extracts structured evidence,
entities are resolved (and unresolved evidence handled — never silently
dropped), evidence is synthesised with quantitative predictions into an
`IntelligenceReport`, and alerts are sent to the user.

The layer is **additive and offline-testable**, honouring the same constraints
as Phase 9.5 / 9.6:

* it does **not** modify the quantitative Phases 1–8 stack — the verifiers only
  consume the existing connectors, `ingest_raw_text`, the Phase 9.4 bridge and
  the Phase 9.6 `Scheduler`;
* it makes **no live API calls inside `pytest`** — connectors inject
  `httpx.MockTransport`-backed HTTP clients and the default LLM provider is the
  offline `MockLLMProvider`;
* it **hardcodes no API keys** — `--provider real` reads the git-ignored `.env`;
* it performs **no aggressive scraping** — rate-limited RSS polling and the
  official FPL API only.

### 20.2 Components

All code lives in `src/fpl_intelligence/live_intelligence/verification/`:

| Module | Component | Responsibility |
|--------|-----------|----------------|
| `live_verification.py` | `RSSFeedVerifier` | Fetches one live RSS feed, checks accessibility (typed connector errors become a failed `connectivity` stage) and parse quality, then pushes every item through `ingest_raw_text` (Phase 9.2). Returns a `LiveSourceVerification` with `fetched` / `parsed` / `ingested` / `duplicates` counts and per-stage pass/fail. |
| `live_verification.py` | `FPLAPIVerifier` | The same contract for the official FPL `bootstrap-static` endpoint (player `news` + `chance_of_playing_*` risk). Fully-available players produce no item and are reported as such. |
| `live_verification.py` | `EndToEndVerifier` | Runs one full pipeline pass over injected connectors via the Phase 9.6 `Scheduler` (fetch → ingest → alert → notify with a `RecordingNotifier`), then adds the Phase 9.4 `AnalystReportGenerator` stage (synthesize → report) over the freshly-committed evidence. Reports `EndToEndVerification` with per-connector totals, extraction-run / evidence / resolution counts, report citations, alerts and notifications delivered. |
| `live_verification.py` | `build_verification_session` | Builds the verification DB — a shared (StaticPool) in-memory SQLite by default, or a file-backed engine via `--db`. `persist=True` commits; `--dry-run` rolls back. |

CLI entry points:

| Script | Verifies |
|--------|----------|
| `scripts/verify_live_rss.py` | Live RSS feed accessibility, parsing and Phase 9.2 ingestion. |
| `scripts/verify_live_fpl_api.py` | Live FPL API accessibility, parsing and Phase 9.2 ingestion. |
| `scripts/verify_live_end_to_end.py` | The full live pipeline (all six stages). |

### 20.3 Data Flow

```
RSS feed / FPL bootstrap-static            (network)
        │
        ▼
Connector.fetch() ──► list[RawItem]        (Phase 9.5, rate-limited, typed errors)
        │
        ▼
Scheduler.run()                            (Phase 9.6, one pass, per-stage error isolation)
        │  ingest sink = ingest_raw_text   (Phase 9.2: hash → dedupe → ledger → extract → persist)
        ▼
LLM extraction (provider injectable; mock by default, --provider real for a live LLM)
        │
        ▼
Entity resolution + UnresolvedLiveEvidence (resolved / unresolved / ambiguous counts)
        │
        ▼
AnalystReportGenerator.generate(...)       (Phase 9.4: evidence × quantitative predictions → report)
        │
        ▼
AlertGenerator → NotificationService → RecordingNotifier (alerts + delivered receipts)
        │
        ▼
EndToEndVerification (per-stage PASS/FAIL + totals)
```

### 20.4 CLI Usage

```bash
# RSS feed verification (BBC team feed, default)
python scripts/verify_live_rss.py

# FPL API verification
python scripts/verify_live_fpl_api.py

# Full end-to-end pipeline verification (all connectors)
python scripts/verify_live_end_to_end.py

# RSS only, custom feed, fetch-but-do-not-persist (safe live smoke test)
python scripts/verify_live_end_to_end.py --connector rss --rss-url https://... --dry-run

# Full pipeline with a real LLM + season/gameweek deadline context
python scripts/verify_live_end_to_end.py --provider real --season-code 2025-26 \
    --gameweek 3 --db ./fpl.db

# Persist into a named DB
python scripts/verify_live_fpl_api.py --db ./fpl.db
```

Exit codes: `0` all checks passed, `1` usage/configuration error, `2`
verification/provider/network failure.

### 20.5 Testing & Quality

- `tests/unit/test_phase9_7_verification.py` — **16 tests** covering
  `RSSFeedVerifier` (accessible feed parse + ingest, connectivity failure,
  invalid XML, duplicate detection, dry-run), `FPLAPIVerifier` (parse + ingest,
  connection failure, invalid JSON, fully-available-player filtering) and
  `EndToEndVerifier` (full pipeline all stages pass, report synthesised with the
  quantitative baseline, alerts generated + delivered, fetch-failure isolation,
  single-connector run, dry-run without report, report `to_dict`). All HTTP is
  mocked with `httpx.MockTransport` and the evidence DB is a shared in-memory
  SQLite — **zero live network calls in `pytest`.**
- **Full suite: 616 passed** (Phase 9.7 adds 16 tests to the 600 collected
  before it).
- `ruff` and `mypy` clean on the `verification/` package, the three CLI scripts,
  and the test module.
- **No migration required.** Phase 9.7 introduces no new tables, columns, or
   enums — it consumes the existing Phase 9.2 pipeline, Phase 9.4 bridge and
   Phase 9.6 scheduler.

---

## 21. Phase 9.8 — Production Deployment

### 21.1 Purpose

Phase 9.8 packages the system for a production environment. It does **not**
modify the quantitative Phases 1–8 stack and adds no new database tables, columns
or enums — it is a pure deployment/operations layer that wraps the existing
Phase 9.2 → 9.7 machinery. The layer has five concerns:

1. **Docker containerization** — a production-ready `Dockerfile` plus a validator
   and a (mockable) build pipeline.
2. **Production configuration** — a YAML config file *plus* environment variables
   for secrets, loaded deterministically.
3. **Monitoring and logging** — metric + health registries, threshold alerting
   (log + webhook sinks) and JSON logging.
4. **Error handling and recovery** — retry with exponential backoff, a circuit
   breaker, and a recovery manager with dead-lettering.
5. **Deployment runner** — turns the above into offline readiness checks and,
   optionally, an image build.

All seams (Docker builder, webhook HTTP client, clocks, sleeps) are injectable, so
**no live API/`docker`/network call happens inside `pytest`** and no API key is
hardcoded.

### 21.2 Components

All code lives in `src/fpl_intelligence/deployment/`:

| Module | Component | Responsibility |
|--------|-----------|----------------|
| `config.py` | `ProductionConfig`, `load_production_config`, `validate_production_config` | Pydantic-Settings model; loads `config/production.yaml` + environment, forces `app_env="production"`, validates production constraints (PostgreSQL, retry/breaker bounds). Secrets are **env-only** (`SECRET_FIELDS`), redacted in `to_dict()`. |
| `docker.py` | `validate_dockerfile`, `DockerBuilder`, `SubprocessDockerBuilder`, `build_docker_image` | Pure string analysis of a `Dockerfile` (pinned base image, `WORKDIR`, `EXPOSE`, non-root `USER`, `CMD`/`ENTRYPOINT`); a `DockerBuilder` Protocol whose production impl shells out to the `docker` CLI via an injectable runner. |
| `monitoring.py` | `MetricRegistry`, `HealthRegistry`, `AlertManager`, `AlertRule`, `AlertSink` (`LogAlertSink` / `WebhookAlertSink`), `ProductionJsonFormatter`, `MonitoringService`, `build_monitoring_service` | Counters/gauges + per-component health + threshold rules over metrics with cooldown dedup; one-line JSON log records; webhook sink uses an injectable `httpx.Client`. |
| `resilience.py` | `RetryPolicy`, `retry`, `CircuitBreaker`, `RecoveryManager`, `RecordingDeadLetterSink` | Exponential backoff (jitter, injectable `sleep`/`clock`), state machine breaker (closed/open/half-open), and a coordinator that gates ops behind the breaker, retries them, dead-letters permanent failures and reports on a `RecoveryReport`. |
| `runner.py` | `DeploymentRunner`, `deploy` | Runs the offline readiness probes (config load/valid, Dockerfile production-ready, monitoring wired) and optionally builds the image; returns a `DeploymentReport`. |
| `scripts/deploy.py` | CLI | `--check-only` (default, fully offline readiness check) and `--build` (readiness + image build). Exit codes: `0` ready/build ok, `1` usage/configuration error, `2` deployment error. |

### 21.3 Configuration contract

* **The file carries no secrets.** `slack_webhook_url`, `smtp_username`,
  `smtp_password` and `critical_error_webhook_url` are **only** ever read from
  environment variables (a value written into the YAML is ignored).
* **Environment overrides the file.** Every field maps to an `UPPER_SNAKE`
  variable via `ENV_FIELD_MAP`; `app_env` is always forced to `production`.
* **Deterministic & mockable.** `load_production_config(path, environ=...)` accepts
  an explicit environment mapping and file path, so tests never touch the real
  `os.environ` / `.env`.
* **Validated before use.** `validate_production_config` enforces PostgreSQL-only
  and sane retry/breaker bounds and feeds the readiness report.

### 21.4 Docker validation invariants

`validate_dockerfile` is pure string analysis (no daemon) and requires:

* a `FROM` with a **pinned** base image (digest or non-`latest` tag),
* a `WORKDIR`,
* an `EXPOSE`,
* a non-root `USER` (rejects `USER root`),
* at least one of `CMD` / `ENTRYPOINT`.

`build_docker_image` validates first (aborts with `DockerError` on any issue) then
builds through the injected `DockerBuilder`, so the build itself is fully mocked
in tests.

### 21.5 Monitoring & alerting

`MonitoringService` exposes `record_metric`, `report_health`, `check_alerts` and
`report_critical_error`. `build_monitoring_service` wires three shipped rules that
watch operational counters maintained by the pipeline:

* `health_all_ok` — fires when `health_checks_failed >= 1` (critical),
* `ingest_failures` — fires when `ingest_failures_total >= 5` (warning),
* `scheduler_errors` — fires when `scheduler_errors_total >= 10` (critical).

The log sink is always present; a `critical_error_webhook_url` adds a webhook
sink. `AlertManager` dedupes persistent breaches by a `cooldown_seconds` window so
a stuck failure alerts once per interval.

### 21.6 Error handling & recovery

* `RetryPolicy.delay_for(failed_attempts)` computes
  `min(max_delay, base * multiplier ** (failed_attempts-1))` with optional uniform
  jitter; `retry()` returns a `RetryOutcome` and never raises for retryable
  exhaustion (a non-retryable exception aborts immediately).
* `CircuitBreaker` opens after `failure_threshold` consecutive failures and
  rejects calls with `CircuitOpenError` until the `reset_timeout_seconds` window
  passes, then allows one half-open trial that closes or reopens the circuit.
* `RecoveryManager.execute(operation_id, op)` gates `op` behind the breaker,
  retries on failure, records a `RecoveryEntry`, and on permanent failure writes
  to the `DeadLetterSink` (e.g. `RecordingDeadLetterSink`) before re-raising
  (or returning `None` with `raise_on_failure=False`).

### 21.7 Data flow (deployment pass)

```
config/production.yaml + environment
        |  load_production_config (secrets env-only, redacted)
        v
ProductionConfig  --validate_production_config-->  readiness: config_load / config_valid
        |
        v
validate_dockerfile(Dockerfile)  -->  readiness: dockerfile (production-ready?)
        |
        v
build_monitoring_service(config)  -->  readiness: monitoring (registries + alert rules)
        |
        v
[optional] build_docker_image(config, builder)  -->  DeploymentReport.build
        |
        v
DeploymentReport (config, dockerfile_ok, readiness, build, validation)
```

### 21.8 Testing & Quality

* `tests/unit/test_phase9_8_config.py` — 20 tests: defaults, YAML + env override,
  secrets-env-only, redaction, forced production `app_env`, PostgreSQL-only
  validation, validator ranges, secret-free repo config file, determinism.
* `tests/unit/test_phase9_8_docker.py` — 24 tests: `DockerBuildConfig.image_ref`,
  build success/failure (fake builder + fake subprocess runner), invalid-Dockerfile
  abort, validation-skip flag, parser accepts/rejects every required directive,
  unpinned/root rejections, digest-pin acceptance, repo Dockerfile passes,
  build-arg/flag assembly. **Zero docker daemon calls — the builder is mocked.**
* `tests/unit/test_phase9_8_monitoring.py` — ~40 tests: counter/gauge
  register/increment/set, registry snapshot isolation, health ok/down/summary,
  alert-rule above/below/threshold, manager fires/skips/missing-metric/cooldown
  dedup/per-sink error isolation, JSON formatter + `setup_production_logging`,
  `MonitoringService` wiring, `build_monitoring_service`. **Webhook sink uses
  `httpx.MockTransport` — no network in `pytest`.**
* `tests/unit/test_phase9_8_resilience.py` — 23 tests: `RetryPolicy` validation,
  exponential-backoff math + cap + jitter, `retry()` first-attempt success,
  retry-then-succeed, exhaustion, non-retryable abort, injected clock; circuit
  breaker closed/open/half-open recovery/reopen/reset/stats; recovery manager
  success, retry-then-recover, failure dead-letter + re-raise, no-raise returns
  `None`, circuit-open path, `RecoveryReport`/`RecoveryEntry` `to_dict`, recording
  dead-letter sink. **No wall-clock sleeps — clocks/sleeps injected.**

Full suite after Phase 9.8: **699 passed, 0 failed** (was 616). `ruff` and `mypy`
clean on the `deployment/` package. **No migration required** — Phase 9.8
introduces no new tables, columns, or enums.

