# Phase 4.75 — Real vs Mock Report

_Generated 2026-08-03T12:47:14.280130+00:00_

## Data coverage

- Real seasons imported: ['2022-23', '2023-24', '2024-25', '2025-26']
- Real rows built: 77841
- Mock rows built: 2520

## Baseline performance comparison

| Model | Real MAE | Real Spearman | Mock MAE | Mock Spearman |
|---|---|---|---|---|
| baseline_a | 1.1151 | 0.6931 | 2.5169 | 0.8115 |
| baseline_b | 1.1038 | 0.6771 | 2.25 | 0.9845 |
| baseline_c | 1.0192 | 0.698 | 2.8963 | 0.9316 |

## Minutes model (start ECE)

- Real start ECE: 0.0076
- Mock start ECE: 0.0

## Contamination / leakage

- synthetic_contamination: PASS
- fixture_ordering_2022-23: ok
- fixture_ordering_2023-24: ok
- fixture_ordering_2024-25: ok
- fixture_ordering_2025-26: ok

## Edge classification

**B**

## Interpretation notes

- Differences are NOT automatically model improvement.
- Possible explanations: synthetic-data bias, real-data noise, missing features,
  model weakness, provider limitations.