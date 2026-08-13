# Phase 9.2 — Multi-Source Ingestion Foundation

> Status: **INITIALIZED** (2026-08-13). Controlled scaffolding only. No live
> scrapers, no automated API fetchers, no changes to the quantitative Phases
> 1–8 stack.

Phase 9.1 proved the LLM extraction engine can turn unstructured text into
structured Phase 7/8 evidence. Phase 9.2 builds the **controlled foundation**
that lets real-world text (press conferences, news, social posts) enter that
engine safely — with declared provenance and duplicate-proof ingestion — before
any automated acquisition is wired in.

This document specifies the **Source Registry**, the **Reliability Tiers**, the
**Raw Item Ledger**, the **Deduplication Engine**, and the **Temporal Rules**
that govern how a piece of text becomes evidence.

---

## 1. Source Registry

The registry is the single place that knows *what kinds of sources the engine
will accept* and *how much to trust each one*. It is implemented in
`src/fpl_intelligence/live_intelligence/source_registry.py`.

### 1.1 Source types

Every ingested item must declare one of eight `SourceType` values:

| `SourceType` | Meaning | Example |
| ------------ | ------- | ------- |
| `official_api` | Machine-readable official feed | FPL official API |
| `press_conference` | Manager / official press conference | Pre-match PC transcript |
| `club_site` | Club's own website / statement | Club injury update |
| `rss` | News RSS item | Generic news feed |
| `journalist` | Named beat reporter | Tier-1 correspondent |
| `aggregator` | News aggregator / wire | Aggregated feed |
| `social` | Social post (X, etc.) | Unverified rumour |
| `manual` | Manual paste of any of the above | Operator-curated paste |

### 1.2 Reliability tiers

Each source is assigned exactly one ordered `ReliabilityTier`. A **lower number
is more reliable**; the ordering is the whole point — it gives every downstream
consumer (analyst guardrails, weighting, validation-evidence selection) a single
comparable axis of trust, independent of the free-text source label.

| Tier | Name | Typical source | `is_official` | `is_structured` |
| ---- | ---- | -------------- | ------------- | --------------- |
| `TIER_0_OFFICIAL_STRUCTURED` | Official structured API | FPL API | yes | yes |
| `TIER_1_OFFICIAL_UNSTRUCTURED` | Official unstructured | Manager press conference, club site | yes | no |
| `TIER_2_RELIABLE_JOURNALIST` | Reliable journalist | Beat reporters | no | no |
| `TIER_3_AGGREGATOR` | Aggregator / wire | News RSS, aggregators | no | no |
| `TIER_4_SOCIAL_UNVERIFIED` | Social, unverified | Twitter/X rumours | no | no |

### 1.3 Tier classification rules

`SourceRegistry.classify_tier(source_type, *, is_official_club=False)` returns
the canonical tier:

* `official_api` → **TIER_0**
* `press_conference` / `club_site` → **TIER_1**
* `journalist` → **TIER_2**
* `rss` / `aggregator` → **TIER_3**
* `social` / `manual` → **TIER_4** (a manual paste is unverified by default)

**Official-club promotion:** a source that is an official club voice — even via
a loose channel such as `social` — is promoted to **TIER_1** (official
unstructured). Anything already at TIER_0/1 is never downgraded.

**Override:** `SourceRegistry.register(..., reliability_tier=...)` lets an
operator declare a specific tier (e.g. a particular journalist promoted to
TIER_2); the registry stores it verbatim.

### 1.4 Persistence bridge

`SourceRegistry.ensure_source(db, source_id, ...)` creates (or returns) the
matching Phase 9.1 `live_intelligence_sources` row, **idempotently**. The
coarse Phase 7 `SourceReliability` column receives the best-fit projection
(`map_tier_to_reliability`), while the **exact** tier is preserved in the row's
`notes` marker so `load_from_db` can recover it exactly. This keeps the Phase
9.2 vocabulary lossless while reusing the existing registry table.

---

## 2. Raw Item Ledger

The ledger is the single point where externally-captured text enters the
pipeline. Implemented in
`src/fpl_intelligence/live_intelligence/raw_item_ledger.py`.

### 2.1 `RawItem` model

A Pydantic model (`RawItem`) that **cannot** carry an inconsistent temporal
footprint:

| Field | Type | Notes |
| ----- | ---- | ----- |
| `raw_item_id` | `int \| None` | Assigned on persistence |
| `source_id` | `str` | Phase 9.2 source identifier |
| `external_id` | `str \| None` | Provider-side content id |
| `url` | `str \| None` | Source URL |
| `title` | `str` | Display title |
| `content_text` | `str` | The raw text |
| `content_hash` | `str` | SHA-256 (whitespace-normalised) |
| `published_at` | `datetime` (tz-aware) | When the source published |
| `scraped_at` | `datetime` (tz-aware) | When we captured the text |
| `available_at` | `datetime` (tz-aware) | Earliest legitimate access |
| `ingested_at` | `datetime` (tz-aware) | When ledgered |
| `temporal_class` | `str` | see §4 |

`RawItem.create(...)` computes the `content_hash` and defaults `available_at`
to `published_at` when not supplied — the honest minimum (we never claim access
before publication).

### 2.2 Temporal validation (hard, on construction)

A `RawItem` is rejected (Pydantic `ValidationError`) unless **all** hold:

1. Every timestamp is timezone-aware (naive timestamps are rejected outright).
2. `published_at <= scraped_at` — cannot capture before publication.
3. `scraped_at <= ingested_at` — cannot ledger before capture.
4. `available_at >= published_at` — cannot be available before it exists.
5. `available_at <= ingested_at` — cannot claim access before we held it.

This mirrors the Phase 9.1 ledger contract and makes back-/forward-dating
structurally impossible at the model level, before anything touches the
database.

---

## 3. Deduplication Engine

`RawItemDeduplicator` guarantees the same `(source_id, content_hash)` is
processed **at most once**:

* **Ground truth:** the Phase 9.1 ledger's unique `(source_id, content_hash)`
  constraint on `live_intelligence_raw_items`.
* **Front cache:** an optional in-memory `set` so a tight loop that re-reads the
  same page does not issue a query per item.
* **Write-time safety:** `RawItemLedger.persist` returns `None` on a duplicate,
  and `ingest_raw_text` returns `ManualIngestStatus.DUPLICATE` with no extraction
  performed — re-submitting identical text from the same source is a clean
  no-op.

The manual script logs **"Duplicate content detected, skipping extraction"** and
exits `0` when a duplicate is found.

---

## 4. Temporal Rules

`temporal_class` uses the Phase 9.2 vocabulary, projected onto the Phase 9.1
enum by `map_temporal_class`:

| `RawTemporalClass` | Meaning | Phase 9.1 mapping | Usable pre-deadline? |
| ------------------ | ------- | ----------------- | -------------------- |
| `pre_deadline` | Info available before the GW deadline | `pre_deadline` | **yes** |
| `post_match` | Content about a match that has finished | `post_deadline` | no |
| `post_deadline` | Info available after the GW deadline | `post_deadline` | no |
| `no_deadline_context` | No gameweek resolved yet | `no_deadline_context` | undecided |

When a `--season-code` / `--gameweek-number` is supplied to the manual script,
the row's deadline is resolved and its class is computed with the existing Phase
3 `InformationAccessPolicy` (`STRICT_REPRODUCIBILITY` by default), exactly as
the Phase 9.1 ledger does.

**Provenance bridge.** The `RawItem`'s `source_id`, `published_at` and
`available_at` are inherited by the extracted evidence through the
`LedgerItemView`: the Phase 9.1 extractor copies them onto every
`AvailabilityEvidenceDraft` / `TacticalEvidenceDraft` and
`persist_extraction` writes them into the Phase 7 `availability_evidence` and
Phase 9 `tactical_evidence` tables. The model never supplies a timestamp, so the
no-look-ahead guarantee holds end-to-end.

---

## 5. Manual Ingestion Script

`scripts/manual_ingest_raw_text.py` is the controlled entry point.

```
python scripts/manual_ingest_raw_text.py \
    --source-id press_conference_manual \
    --file transcript.txt \
    --published-at 2025-08-15T14:00:00+01:00 \
    --url https://example.com/transcript

python scripts/manual_ingest_raw_text.py \
    --source-id journalist_manual \
    --text "Salah is ruled out for the next three weeks." \
    --published-at 2025-08-15T14:00:00Z

python scripts/manual_ingest_raw_text.py \
    --source-id press_conference_manual --file transcript.txt \
    --published-at 2025-08-15T14:00:00Z \
    --season-code 2025-26 --gameweek-number 3

python scripts/manual_ingest_raw_text.py \
    --source-id press_conference_manual \
    --file scripts/fixtures/press_conference_transcript.txt \
    --published-at 2026-08-14T09:00:00Z \
    --dry-run
```

The `--dry-run` example uses the bundled fixture transcript and the deterministic
`MockLLMProvider` (the default). It exercises the full extraction pipeline
without making network calls or writing to the database.

Arguments: `--source-id` (required), `--file` *or* `--text` (required),
`--published-at` (required, ISO-8601), `--url`, `--external-id`, `--title`,
`--source-type`, `--available-at`, `--temporal-class`, `--season-code`,
`--gameweek-number`, `--provider mock|real`, `--db` (SQLite path; defaults to
in-memory), `--dry-run`.

`--dry-run` performs extraction and persistence inside a transaction and rolls
back at the end. Counts and IDs are printed as if the run had been committed, but
no rows are permanently written. This is safe to run against any database
(including the live `fpl` PostgreSQL) because all changes are discarded.

Defaults to the deterministic `MockLLMProvider` (no network calls, no quota).
`--provider real` builds a guarded real provider from `.env` settings.

Pipeline: `text + published_at` → SHA-256 hash → duplicate check → `RawItem`
persisted to the ledger → projected into a `LedgerItemView` →
`PromptedLLMExtractor` (Phase 9.1) → `persist_extraction` → Phase 7/8 evidence
tables → printed summary + evidence ids.

---

## 6. Entity Resolution Bridge & Unresolved Evidence (9.2.1)

Phase 7 was blocked by a **provider-key namespace mismatch**: the historical
importer resolved players against `real_fpl` while the live ingestion path used
`real_fpl_bootstrap` (and other aliases). Both really mean "the FPL `element`
id". Phase 9.2.1 closes that gap with a tolerant, auditable resolution bridge
implemented in `src/fpl_intelligence/live_intelligence/entity_resolution.py`.

### 6.1 Provider-key normalization

`PROVIDER_KEY_ALIASES` maps every known FPL namespace onto a single canonical
key `fpl_element`:

| Incoming alias | Canonical key |
| -------------- | ------------- |
| `real_fpl` | `fpl_element` |
| `fpl` | `fpl_element` |
| `real_fpl_bootstrap` | `fpl_element` |
| `live_intelligence` | `fpl_element` |
| `fpl_bootstrap` / `fpl_official` | `fpl_element` |
| *(unknown)* | *identity (passed through)* |

`canonical_provider_key(name)` returns the canonical key (identity for unknown
names, so a genuinely new namespace is never silently dropped). Authoritative
element-ID lookups use `PlayerExternalId` / `TeamExternalId` rows whose
`provider` is the canonical `fpl_element` key. The seeding helper
`seed_fpl_external_id` stores/aliases a provider player/team id under
`fpl_element` so the `real_fpl` vs `real_fpl_bootstrap` mismatch no longer
breaks resolution. Seeding is **application-side**, not a migration, because the
canonical mapping depends on runtime-ingested player rows.

### 6.2 Resolution priority

`build_entity_resolver(db)` returns a callable with signature
`(name, team=None, *, external_id=None, season_id=None, kind="player")
-> ResolutionResult`, where `ResolutionResult(status, canonical_id, reason)`
carries the audit status. Priority chain:

1. **Explicit external id** (`fpl_element:123`) → `RESOLVED_BY_EXTERNAL_ID`
2. **Canonical player id** (provider == canonical key) → `RESOLVED_BY_EXTERNAL_ID`
3. **Normalized full name + team context + season membership** → `RESOLVED_BY_NAME_TEAM`
4. **Normalized full name only if unique among active players** → `RESOLVED_BY_NAME_UNIQUE`
5. **Alias/fuzzy match only with high confidence + explicit audit reason** → `RESOLVED_BY_ALIAS`
6. Otherwise `UNRESOLVED_PLAYER` / `UNRESOLVED_TEAM` / `AMBIGUOUS_PLAYER`

"Active" approximates "appears in `PlayerTeamMembership` for the resolved
`season_id`" when a season is supplied, else "unique normalized name across all
players". Ambiguity is reported, never guessed.

### 6.3 Audit statuses

`ResolutionStatus`: `RESOLVED`, `RESOLVED_BY_EXTERNAL_ID`,
`RESOLVED_BY_NAME_TEAM`, `RESOLVED_BY_NAME_UNIQUE`, `RESOLVED_BY_ALIAS`,
`UNRESOLVED_PLAYER`, `UNRESOLVED_TEAM`, `AMBIGUOUS_PLAYER`.

### 6.4 Unresolved evidence persistence (never silently dropped)

`persist_extraction` writes a `UnresolvedLiveEvidence` row (Phase 9-owned, in
`live_intelligence/models.py`) for **each** unresolved/ambiguous draft, in
addition to the existing JSON `unresolved_entities` on the extraction run. The
raw item itself is persisted by `ingest_raw_text` *before* extraction, so provenance
back to the source text always survives even when no canonical entity exists.
The run tracks `resolved` / `unresolved` / `ambiguous` counts, and
`ManualIngestReport` exposes `resolved_count` / `unresolved_count` /
`ambiguous_count` / `unresolved_evidence_ids`. The manual script prints these.

### 6.5 Migration

`0012_phase921_unresolved_evidence.py` creates `unresolved_live_evidence` (plus
the native `resolutionstatus` enum, created once idempotently) with FKs to the
Phase 9.1 ledger / extraction-run tables. **No Phase 7 table is touched.**

---

## 7. Testing & Quality

* **19 new Phase 9.2 tests** in `tests/unit/test_phase9_2_ingestion.py` (source registry, RawItem validation, deduplication, manual ingestion + script).
  * Source Registry tier classification (all 8 types, official-club promotion,
    tier flags, override, source-type/tier mappings, DB round-trip).
  * `RawItem` temporal validation (hash + `available_at` default, whitespace
    normalisation, `available_at < published_at` rejection, naive-datetime
    rejection).
  * Deduplication (hash + skip, in-memory cache front, duplicate skip in
    `ingest_raw_text`).
  * Manual ingestion + extraction bridge (extracts & persists evidence; the
    persisted `availability_evidence.valid_from` equals the `RawItem`
    published_at; inconsistent temporal rejected).
  * Manual ingestion **script** using a mock file and `MockLLMProvider`,
    including the clean duplicate-skip path.
* **16 new Phase 9.2.1 tests** in `tests/unit/test_phase9_2_1_entity_resolution.py` (provider-key aliases, external-id/name/unique/ambiguous resolution, unresolved player & team persistence, provider-key mismatch tolerance, duplicate skip, raw-item-survives-unresolved).
* Full suite: **462 passed** (was 446).  `ruff` and `mypy` clean on the new modules.
