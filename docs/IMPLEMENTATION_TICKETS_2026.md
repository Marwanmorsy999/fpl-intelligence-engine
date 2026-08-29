# FPL Intelligence Engine — Implementation Tickets

Date: 2026-08-27

These tickets are intentionally small enough for an agent to execute without re-researching the whole project.

## Gate 0 — Audit/Guardrails

- Read `docs/AUDIT_2026.md`, `docs/RESEARCH_2026.md`, `docs/DATA_SOURCE_MATRIX.md`, `docs/DATA_POLICY.md`.
- Confirm current code matches the docs before edits.
- Do not rewrite unrelated systems.
- Establish a failing-test-before-fix baseline where a relevant bug exists.

## Gate 1 — Provider layer

1. Define canonical provider capability/metadata contract.
2. Implement/normalize Official FPL provider.
3. Implement API-Football secondary adapter.
4. Implement Open-Meteo adapter as optional enrichment.
5. Preserve existing Football-Data/The Odds configuration if used, but route through the registry.
6. Add provider priority, quota, freshness and cache metadata.
7. Add central retry/rate-limit/circuit-breaker behavior.
8. Record provider provenance on raw ingestion.

## Gate 2 — Temporal data integrity

1. Audit current `FeatureRegistry` cutoff behavior.
2. Audit every feature calculator for cutoff compliance.
3. Fix any historical `ingested_at` misuse.
4. Separate published/available/fetched timestamps.
5. Add temporal classification.
6. Block strict-backtest features with insufficient timestamp provenance.
7. Add leakage tests.
8. Fix the known `real_fpl` vs `real_fpl_bootstrap` entity-resolution mismatch before relying on availability evidence.

## Gate 3 — Prediction stack

Implement/validate in this order:

1. expected minutes
2. team strength
3. goal distribution
4. assist distribution
5. clean-sheet probability
6. bonus/BPS probability
7. defensive contribution
8. GK saves
9. cards
10. FPL-rule point transformation
11. player expected-points distribution
12. uncertainty intervals
13. calibrated ensemble

For each model store version, training window, feature version and metrics.

## Gate 4 — Validation

1. Compare against naïve baselines.
2. Walk forward by Gameweek/season.
3. Run calibration/Brier/log-loss checks for probabilities.
4. Run final untouched holdout only after model freeze.
5. Test Monte Carlo convergence where used.
6. Create machine-readable validation report.

## Gate 5 — Decision engine

Centralize all decisions behind one service layer:

- transfer vs hold
- hit vs no-hit
- captain choices
- bench order
- differential discovery
- price-risk
- wildcard
- free hit
- bench boost
- triple captain
- 4/6/8 GW plans
- rotation
- rank attack
- rank defense

Every decision result must contain recommendation, projected impact, risk, confidence, alternatives and evidence IDs.

## Gate 6 — Intelligence signals

Add only after provider/temporal foundations are stable:

- news
- injury/availability
- training-return
- predicted starts
- tactical role
- set pieces
- manager/rotation tendencies
- ownership/EO
- price pressure

Do not use post-deadline confirmed lineups as historical pre-deadline features.

## Gate 7 — Product consolidation

Do not add more top-level pages unless necessary.

Consolidate existing routes into:

- Home
- My Team
- Transfers
- Players
- Planner
- Live
- League
- AI

Expose advanced evidence/details within each view.

## Gate 8 — AI analyst

Structured tools only:

- `get_team`
- `get_player`
- `get_predictions`
- `get_fixtures`
- `get_news`
- `simulate_transfer`
- `simulate_captain`
- `simulate_chip`
- `simulate_plan`
- `get_rivals`
- `get_live_rank`

The LLM explains structured outputs; it does not create the numerical truth.

## Gate 9 — Release

Required before release:

- tests pass
- type/lint checks pass
- migrations pass
- ingestion smoke test passes
- production API smoke test passes
- frontend build passes
- no secret exposure
- no critical console errors
- stale/fallback states work
- temporal leakage tests pass
- docs match implementation

## Stop conditions

Stop and report instead of improvising when:

- a provider's current terms are unclear
- required timestamps cannot be reconstructed
- a model only improves in-sample
- a dependency requires paid production access contrary to free-first policy
- an existing public contract would be broken
- a data source becomes unavailable

The next task is always the smallest safe task that moves one gate forward.
