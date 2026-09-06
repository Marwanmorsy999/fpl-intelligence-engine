# Project Status

**Last verified:** 2026-09-06

This file is a compact operational status record. Detailed historical phase reports and generated proof bundles are not treated as authoritative runtime state.

## Current state

- **Production branch:** `main`
- **Integration branch:** `master`
- **Current cleanup branch:** `chore/cleanup-2026-rules`
- **Runtime:** Python 3.12 + FastAPI
- **Persistence:** PostgreSQL + SQLAlchemy/Alembic
- **Rule source:** `config/fpl_rules/2026-27.yaml`

## 2026/27 FPL rule alignment

The season configuration now carries the published 2026/27 scoring constants, including goalkeeper goals (10), defender goals (6), midfielder goals (5), forward goals (4), assists (3), goalkeeper/defender clean sheets (4), midfielder clean sheets (1), appearance scoring, saves, penalty saves/misses, cards, own goals, goals-conceded deductions, and defensive-contribution thresholds.

The configuration also records the 2026/27 chip structure, five-transfer rollover cap, and the later 09:00 UK Gameweek lockdown.

## Validation and CI

`.github/workflows/ci.yml` is the repository test gate for pushes and pull requests targeting `main` or `master`. It performs:

1. Python environment setup
2. editable install with development dependencies
3. Playwright Chromium installation
4. source compilation
5. Ruff linting
6. the full pytest suite
7. FastAPI import smoke test

This document does not claim that the full suite is green until a current CI run demonstrates it.

## Cleanup policy

Generated gate/proof snapshots, temporary files, local database/runtime state, IDE metadata, and other machine-local artifacts must remain out of Git. Durable evidence belongs in focused tests, source documentation, issues, or pull requests.

## Known structural risk

`main` and `master` have historically diverged significantly. Changes should therefore move through focused pull requests rather than attempting to reconcile branches by force-resetting shared history.

## Next engineering priorities

- Complete the branch reconciliation through reviewed PRs.
- Keep season rules versioned and validated against official FPL sources.
- Continue temporal/no-look-ahead validation before claiming historical model performance.
- Keep generated audit artifacts out of the repository unless a specific durable artifact is genuinely required.
