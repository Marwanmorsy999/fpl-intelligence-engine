# Phase 15.0 — Live Prediction Chain

## Status

**SHIPPED** (v1.5.0-real-intelligence). Replaces the hardcoded
`StaticPredictionProvider` stub (5.5 xPTS for every player) in production with a
transparent, labelled fallback chain backed by the official FPL bootstrap, an
Understat xG/xA snapshot, and optional market-implied probabilities.

## The chain (resolution order)

For each gameweek the provider scores every level **best-first** and serves the
last level that produces numbers. Every prediction carries its `source` and
`data_quality` label so the UI can never present a heuristic as a computed model.

| Priority | Level              | Source constant        | Data quality                | What it is |
|----------|--------------------|------------------------|-----------------------------|------------|
| 1        | model-backtest     | `model-backtest`       | `historical-backtest`       | Stored player predictions from the latest *successful* backtest run (per-gameweek JSON, or `PlayerPrediction` rows). |
| 2        | baseline-model     | `baseline-model`       | `ingested-gameweek-history` | Recency-weighted recent-form baselines over ingested `PlayerGameweekPerformance` history. Coverage-gated: needs ≥ 25 % of the player universe. |
| 3        | pre-season-proxy-v2| `pre-season-proxy-v2`  | `heuristic-proxy-enriched`  | Transparent heuristic: price-percentile base rate, enriched with Understat last-season xG/xA per-90 + minutes share, a labelled market-probability bump for favourites, and a negative adjustment ONLY under severe forecast weather. |

The **static stub** (`StaticPredictionProvider`) is *never* used inside this
provider. It exists only behind the explicit `PREDICTION_PROVIDER=static` switch
(for unit tests / `--dry-run`), and **production startup refuses it** — see
[below](#production-safety).

## The proxy formula (transparent, no hidden randomness)

```
base  = 0.8 + 6.2 * price_pct ^ 1.7
x90   = min(3.0, 1.05 * xG90 + 0.75 * xA90)        # from the Understat snapshot
pts   = base + x90 * minutes_share                  # threat
      + 0.4 * p_win                                 # market favourites only
      + weather_adj                                # severe forecasts only
pts   = clamp(pts, 0.4, 13.0)
```

Each enrichment signal degrades independently: a missing Odds key, unreachable
weather, or an unmatched Understat name simply drops its term and is recorded
in the chain `notes` — the request **never** fails because of an upstream
enrichment problem.

## Enrichment sources

| Source                  | Key required | Access | Role | Free-tier reality |
|-------------------------|--------------|--------|------|-------------------|
| **FPL official bootstrap** | none | `fantasy.premierleague.com/api/bootstrap-static/` (one fetch, written to `data/seed/fpl_bootstrap_seed.json`) | Authoritative teams, positions, prices (`now_cost/10`), element codes for PL CDN photos. The committed seed is the single source of truth for prices. | Public endpoint, but **Vercel/Cloud datacenter IPs are routinely blocked** — which is why the committed seed exists and is refreshed locally via `_fetch_bootstrap_seed.py`. |
| **Understat** (xG/xA)   | none | `understat.com/main/getPlayersStats/` AJAX, hex-encoded inline JSON, cached 24 h with ≥ 1 s politeness. Offline snapshot at `data/seed/understat_snapshot.json`. | Last-season per-90 xG/xA + minutes share feed the `x90 * minutes_share` term and the per-player minutes/start estimates. | Free, no key. Upstream occasionally blocks hosting IPs, so the committed snapshot guarantees baseline enrichment. |
| **The Odds API** (market-implied probabilities) | `THE_ODDS_API_KEY` (optional) | `the-odds-api.com` — h2h market implied probabilities per team per GW. | Small labelled `0.4 * p_win` bump, applied ONLY to favourites the market prices accordingly. Surfaced in the UI as "Market check: agrees/disagrees". | Free tier: **500 requests/month**. Each GW sweep that matches fixtures consumes a handful. With no key the signal is simply disabled (recorded in `chain.market_check.enabled = false`). |
| **Open-Meteo** (severe weather) | none | `open-meteo.com` forecast API. | Negative adjustment ONLY when a player's fixture is under a severe forecast. | Free, no key, generous limits. Degrades to "no adjustment" when unreachable. |

## Provenance in the API / UI

`GET /api/v1/decisions` now returns, for every player:

```json
{
  "players": {
    "123": {
      "expected_points": 6.42,
      "prediction_source": "pre-season-proxy-v2",
      "data_quality": "heuristic-proxy-enriched",
      "minutes_estimate": 71.3,
      "start_prob": 0.82,
      "xg": 0.41,
      "xa": 0.27
    }
  },
}
```

- `xg` / `xa` are **only present for Understat-matched players** — the dashboard
  renders xG/xA lines exclusively for them, never fabricating values.
- Players absent from the resolved chain are **omitted from xPTS display**, never
  invented.

And a top-level `meta.chain` object:

```json
{
  "meta": {
    "chain": {
      "source": "pre-season-proxy-v2",
      "source_label": "Pre-season proxy v2 (price + fixtures + xG + market)",
      "data_quality": "heuristic-proxy-enriched",
      "covered_players": 599,
      "market_check": { "enabled": true, "fixtures_matched": 10 },
      "notes": { "understat_players_matched": 320, "weather_severe_fixtures": 2 }
    }
  }
}
```

The dashboard banner surfaces `source_label` + `data_quality`; the captain
spotlight appends a "Market check" line and a weather line **only when the
data exists**.

## Production safety

`src/fpl_intelligence/api/main.py` calls
`assert_no_static_stub_in_production()` at import time. If
`APP_ENV=production` and `PREDICTION_PROVIDER=static`, startup raises immediately
— a deployment that would serve the 5.5-point stub to every user is refused.

The production default is `PREDICTION_PROVIDER=live` (or unset), which resolves
`LivePredictionProvider`. Tests opt into the static stub via the
`PREDICTION_PROVIDER=static` env var or by injecting `StaticPredictionProvider`.

## Verification

- `tests/unit/test_phase15_live_prediction_route.py` — proves the live chain
  resolves, xPTS are **differentiated** (not the flat 5.5 stub), every
  `PlayerDetail` carries `prediction_source`/`data_quality`/`minutes_estimate`/
  `start_prob`, `meta.chain` carries provenance, and the stub is only used when
  forced.
- `tests/unit/test_dashboard_static_assets.py` — `node --check` on the inline
  script plus a functional check that the chain banner renders the right label
  per source and stays empty when there is no chain object.
- Full suite: `ruff` clean, `mypy` clean, all `pytest` green.
