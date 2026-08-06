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

---

## 13. Remaining Tasks

1. Wire a real live-scraping source (manual paste is the current input path).
2. Implement entity resolution hook (`resolve_player`, `resolve_team`) that
   matches the canonical Phase 7 provider key.
3. Run empirical backtest once a `STRICT_BACKTEST_SAFE` historical source is
   acquired or live data accumulates past a deadline.
4. Do **not** assign A/B/C until empirical validation is possible.
