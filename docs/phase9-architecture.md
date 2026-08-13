# Phase 9 Architecture — Live Intelligence Accumulator & LLM Analyst Layer

**Status:** IMPLEMENTATION_COMPLETE
**Last updated:** 2026-08-06
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

