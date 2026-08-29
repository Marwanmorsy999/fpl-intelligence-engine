# Stage 2B.2 Data Blocker — Resolved

Status: **RESOLVED (2026-08-29)**.

The earlier connectivity/data blocker is now cleared for the feature-branch validation workflow.

## Resolution evidence

GitHub Actions `Historical Model Validation #7` reached the canonical Supabase database and completed the full validation workflow successfully.

The Team Strength validation source is now present and temporally usable:

- `team_match_performances`: 2,280 historical rows
- 760 rows each for 2022-23, 2023-24, and 2024-25
- 2,280/2,280 rows have both `available_at` and `ingested_at`
- the locked 2025-26 holdout and 2026-27 current-season rows are excluded from Stage 2B validation

The Team Strength evaluator was hardened to fail closed when the source is empty or temporal provenance is incomplete. This eliminates the previous constant-fallback failure mode.

## Validation result

Run #7 produced distinct results for `ewma`, `poisson`, `rolling_goals`, and `rolling_xg` across 1,140 historical fixtures. A regression test also verifies that these methods produce distinct strength and fixture-probability signatures on controlled chronological data.

The current development-season winner is **EWMA**, which is retained as the Team Strength candidate. No production promotion has occurred.

The final 2025-26 holdout remains locked and is not part of this Stage 2B validation.
