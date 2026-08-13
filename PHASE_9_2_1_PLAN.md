# Phase 9.2.1 — Entity Resolution Bridge and Unresolved Evidence Persistence

## Goal
Make live evidence ingestion robust against unresolved entities. Evidence is never silently
dropped; resolution is explicit, auditable, and tolerant of provider-key namespaces.

## Design

### 1. Provider-key normalization (`live_intelligence/entity_resolution.py`, new)
- `PROVIDER_KEY_ALIASES: dict[str, str]` maps alias namespaces to a canonical key
  `fpl_element`. At minimum handles `real_fpl`, `fpl`, `real_fpl_bootstrap`,
  `live_intelligence` -> all resolve to `fpl_element`.
- `canonical_provider_key(name)` returns the canonical key (identity for unknown names).
- Authoritative element-ID lookups use `PlayerExternalId`/`TeamExternalId` rows whose
  `provider` is the canonical `fpl_element` key. Seeding helper `seed_fpl_external_id`
  stores/aliases a provider player/team id under `fpl_element` so that the Phase 7
  `real_fpl` vs `real_fpl_bootstrap` mismatch no longer breaks resolution.

### 2. Entity resolution priority (`build_entity_resolver`)
Returns a `ResolutionResult(status, canonical_id, reason)` instead of `int | None`.
Priority chain:
  1. Explicit external player id (e.g. `fpl_element:123`) -> RESOLVED_BY_EXTERNAL_ID
  2. Canonical player id (provider=canonical key) -> RESOLVED_BY_EXTERNAL_ID
  3. Normalized full name + team context + season/membership context -> RESOLVED_BY_NAME_TEAM
  4. Normalized full name only if unique among active players -> RESOLVED_BY_NAME_UNIQUE
  5. Alias/fuzzy matching only with high confidence + explicit audit reason -> RESOLVED_BY_ALIAS
  6. Otherwise UNRESOLVED_PLAYER / UNRESOLVED_TEAM / AMBIGUOUS_PLAYER
- `ResolutionStatus` enum with the required audit values.

### 3. Unresolved evidence persistence (`UnresolvedLiveEvidence`, Phase 9-owned table)
New table `unresolved_live_evidence` (appended to `live_intelligence/models.py`, no change
to Phase 7 tables):
  raw_item_id, source_id, extraction_run_id, evidence_type, player_name, team_name,
  status_mentioned, quote, confidence, prompt_hash, provider_name, resolution_status,
  resolution_reason, team_hint, created_at.
- `persist_extraction` writes a row for each unresolved/ambiguous draft (in addition to the
  existing JSON `unresolved_entities` on the run) so the raw item survives even when no
  canonical entity exists.

### 4. Wiring
- `extraction.persist_extraction` accepts the new resolver returning `ResolutionResult` and
  persists unresolved rows; tracks resolved/unresolved/ambiguous counts on `PersistenceReport`.
- `raw_item_ledger.ingest_raw_text` exposes resolved/unresolved/ambiguous counts and ids on
  `ManualIngestReport`.
- `scripts/manual_ingest_raw_text.py` prints total drafts, resolved, unresolved, ambiguous,
  evidence ids and unresolved evidence ids.

### 5. Tests (`tests/unit/test_phase9_2_1_entity_resolution.py`)
- resolution by external id (multiple provider namespaces)
- resolution by normalized name + team
- resolution by unique name
- ambiguous name handling
- unresolved player persistence
- unresolved team persistence
- provider-key mismatch tolerance
- duplicate content still skipped
- raw item persisted even when evidence unresolved

### 6. Docs
- `docs/phase9-multi-source-ingestion.md`: resolution priority, provider normalization,
  unresolved policy, audit statuses.
- `docs/PROJECT_STATUS.md`: Phase 9.2.1 status, test count, entity resolution policy,
  Phase 9.3 blocked.

### 7. Migration
New Alembic migration creating `unresolved_live_evidence` and a uniqueness index; no Phase 7
table touched.
