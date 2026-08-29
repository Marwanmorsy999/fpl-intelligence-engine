# FPL Intelligence Engine — Data Policy

Date: 2026-08-27

## Principles

- Prefer official/public sources and open historical datasets.
- Separate access from redistribution rights.
- Keep raw-provider data isolated from derived features.
- Preserve provenance and timestamps for every prediction-affecting input.
- Never use post-deadline information in a pre-deadline backtest.
- Never expose provider secrets to the browser.
- Do not build the product around a source whose free plan is explicitly development-only.

## Required provider metadata

Every provider adapter should declare:

- name
- URL
- capabilities
- cost tier
- authentication type
- quota
- freshness
- historical coverage
- caching policy
- attribution requirement
- redistribution status
- production status
- temporal reliability

## Temporal classification

Each signal/field used by modeling must be classified:

- STRICT_BACKTEST_SAFE — information can be reconstructed as available before the cutoff.
- PRE_DEADLINE_BUT_UNCERTAIN — likely usable but timestamp/data quality needs monitoring; never silently treat as safe.
- POST_DEADLINE_OUTCOME_ONLY — useful for evaluation/live views but forbidden as historical input.
- UNSAFE_LOOKAHEAD — terminal snapshots or later information; forbidden in strict backtests.

## Current source policy

### Official FPL

Primary source for FPL game state and rules. Monitor official rules and terms:
https://fantasy.premierleague.com/help/terms

### Vaastav

Historical training/research source. Inspect every field for leakage before modeling. Do not assume an entire file is temporally safe.
https://github.com/vaastav/Fantasy-Premier-League

### Football-Data.co.uk

Historical match/results/odds research source. Store source and season metadata. Review its current terms before redistribution.
https://www.football-data.co.uk/

### FBref / Understat

Supplemental football analytics. Prefer permitted/cached/derived use. Do not make undocumented scraping the only production dependency.

### Open-Meteo

Optional weather enrichment. Current documentation states the normal free non-commercial API requires no API key and that served data is CC BY 4.0; attribution is required. Commercial/high-volume use has separate conditions.
https://open-meteo.com/

### API-Football

Secondary live/enrichment provider. Current free plan is 100 requests/day and 10 requests/minute. Use caching and quota-aware scheduling. Do not expose the key.
https://www.api-football.com/pricing

### NewsAPI / GNews

Current free plans are development/testing tiers with delayed content and small quotas. Do not make them a production news dependency unless the applicable plan/rights change.
https://newsapi.org/pricing
https://gnews.io/pricing

## Redistribution rule

Before publishing third-party raw content, verify the provider's current terms. Derived scores/features should remain traceable to their source but are not automatically licensed merely because the input was public.

## AI policy

LLMs may summarize and explain permitted data, but must not invent or restate unsupported facts as evidence. Quantitative predictions always come from the engine.

## Enforcement

Provider integrations should fail closed for strict backtesting if temporal provenance is missing. Production live views may use lower-confidence data only when visibly labeled and excluded from historical validation.
