# Availability Point-in-Time — Status

**Branch:** `feature/availability-pit`  
**Production (`main`):** unchanged — do not promote until gates below pass on real coverage.

## What is implemented

| Component | Path | Status |
|-----------|------|--------|
| PIT provider (fplcache snapshots) | `src/fpl_intelligence/availability/historical/pit_fplcache.py` | ✅ |
| Entity-resolution aliases | `entity_resolution.py` (`fplcache_pit` ↔ `real_fpl`) | ✅ |
| Materialize deadline-adjacent snapshots | `materialize_pit.py` + `scripts/materialize_availability_pit.py` | ✅ |
| DB deadline loading | `deadlines.py` (`--from-db-deadlines`) | ✅ |
| Chronological eligibility | `chronological.py` | ✅ |
| Offline signal-lift vs actual minutes | `signal_lift.py` | ✅ |
| End-to-end dry-run evaluate | `scripts/evaluate_availability_pit.py` | ✅ |
| CI workflow | `.github/workflows/availability-pit.yml` | ✅ |
| Phase 7 importer (idempotent) | `importer.py` | ✅ (wired; persist only with `--import --commit`) |
| Coverage audit | `coverage.py` | ✅ (existing) |

## Architectural rules

- **event time ≠ information availability time**
- Snapshot `captured_at` is used as `published_at` / `available_at`
- Unknown timing is never treated as pre-deadline
- Historical records are append-only; import is idempotent on `provider_event_id`
- Default paths are **read-only / dry-run**

## Promotion gates (not yet claimed)

1. Real historical coverage across multiple seasons and gameweeks  
2. Valid information timestamps on all strict-safe events  
3. Successful entity resolution rate on flagged players  
4. Chronological evaluation: eligibility_rate ≈ 1.0 on deadline-adjacent snapshots  
5. Measurable signal: restricted statuses → lower actual minutes than available  
6. Explicit decision to wire into live decision chain (not automatic)

## Commands

```bash
# CI-equivalent dry-run evaluate
python scripts/evaluate_availability_pit.py \
  --cutoff 2024-08-16T16:00:00Z --season 2024-25 --gameweek 1 \
  --cutoff 2025-08-15T16:00:00Z --season 2025-26 --gameweek 1

# Expand from DB deadlines (read-only session)
python scripts/materialize_availability_pit.py \
  --from-db-deadlines --season-code 2024-25 --gw-min 1 --gw-max 10 --evaluate

# Persist (explicit only)
python scripts/materialize_availability_pit.py \
  --from-db-deadlines --season-code 2024-25 --gw-min 1 --gw-max 5 \
  --import --commit
```
