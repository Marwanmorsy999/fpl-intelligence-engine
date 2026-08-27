# FPL Intelligence Engine — Data Source Matrix

Date: 2026-08-27

## Priority tiers

P0 = core / must support
P1 = high-value enrichment
P2 = optional / experiment

| Source | Cost | Key | Best use | Main limitation | Production role | Priority |
|---|---|---:|---|---|---|---:|
| Official FPL public endpoints | Free/public access | No | Canonical FPL player, team, fixture, live and entry data | Endpoint stability/terms must be monitored | Primary FPL truth | P0 |
| Vaastav FPL dataset | Free open dataset | No | Historical FPL GW/player/fixture training data | Some fields can be look-ahead/season-end snapshots; verify per field | Historical training/backtesting | P0 |
| Football-Data.co.uk | Free downloads | No | Historical match results/stats/odds | Coverage/schema varies by season | Historical team model enrichment | P0 |
| FBref | Public web data | No | Player/team football context | Access/usage conditions; not a guaranteed API contract | Supplemental enrichment / research | P1 |
| Understat | Public web data | No | xG/xA-style underlying performance | Mapping/access/usage and timestamp concerns | Research/supplemental; never the sole live dependency | P1 |
| Open-Meteo | Free; no sign-up/API key for normal non-commercial use | No | Weather | Attribution and commercial/high-volume conditions; weather may add little predictive value | Optional enrichment | P2 |
| API-Football | Free tier: 100 req/day, 10 req/min | Yes | Injuries, lineups, events, player/team stats, fixtures | Small free quota; deeper history is plan-limited | Secondary high-value provider with aggressive cache | P1 |
| TheSportsDB | Free tier | Key/public-key model varies | Metadata, teams, players, events | Need to verify current quotas/coverage | Secondary metadata | P2 |
| NewsAPI | Free developer tier: 100 req/day, 24h delay | Yes | Development news ingestion | Explicit development/testing restrictions and delayed articles | Development only unless licensed plan | P2 |
| GNews | Free: 100 req/day, 12h delay | Yes | Development news ingestion | Development/testing plan; delayed articles | Development only unless licensed plan | P2 |
| Existing RSS/news feeds | Often free | Usually No | Live/near-live news | Feed quality/availability/terms differ | Preferred low-cost production news path where permitted | P1 |

## Current verified facts

- API-Football currently advertises a free plan with 100 requests/day and 10 requests/minute; relevant endpoints include lineups, injuries, sidelined players, statistics, fixtures, events and predictions. Source: https://www.api-football.com/pricing and https://www.api-football.com/news/post/how-ratelimit-works
- NewsAPI currently advertises a free Developer plan with 100 requests/day and 24-hour article delay, explicitly for development/testing. Source: https://newsapi.org/pricing
- GNews currently advertises a free tier with 100 requests/day and 12-hour delay, for development/testing. Source: https://gnews.io/pricing
- Open-Meteo documents no API key/sign-up for the normal free non-commercial API, with CC BY 4.0 attribution requirements; commercial/high-volume use has separate conditions. Source: https://open-meteo.com/
- Official FPL added a Price Change Predictor for 2026/27 based on transfer activity and updates around the daily price-change cycle. Source: https://www.premierleague.com/en/news/4680462
- Official FPL changed the BPS for 2026/27; season-versioned scoring rules are mandatory. Source: https://www.premierleague.com/en/news/4679946

## Provider architecture rules

1. Official FPL is the canonical FPL truth wherever possible.
2. Historical data must retain source and temporal classification per field/feature.
3. No provider may be a single point of failure for optional enrichment.
4. API keys remain server-side only.
5. Provider calls are centrally rate-limited and cached.
6. Read quota headers where available and adapt scheduling.
7. A provider outage should degrade to cache/secondary source, not break the UI.
8. Do not use a source in strict backtests unless its information timestamp is defensible.
9. Do not redistribute third-party data until its terms/license have been reviewed.
10. Prefer calculated features over importing redundant third-party rankings.

## Recommended free-first stack

Core:
- Official FPL
- Vaastav historical FPL
- Football-Data.co.uk

High-value enrichment:
- API-Football free quota
- permitted RSS/public team news
- FBref/Understat supplemental data where usage is appropriate

Optional:
- Open-Meteo
- TheSportsDB

Development only unless separately licensed:
- NewsAPI free
- GNews free

## API-Football quota strategy

100 requests/day is too small for page-load fetching across all players. Therefore:

- never call API-Football directly from the browser
- prefetch scheduled datasets
- cache responses by endpoint/parameters
- avoid duplicate calls
- use the 10/minute ceiling centrally
- consume quota first for injuries/lineups/high-impact information
- expose remaining quota internally
- disable low-value jobs when quota is low
- fall back to cached/public data

## Data policy reminder

“Free to access” does not automatically mean “free to republish commercially”. Every provider needs a terms/license review before public redistribution. The agent must record the decision in `docs/DATA_POLICY.md`.
