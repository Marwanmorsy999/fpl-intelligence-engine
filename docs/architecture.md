# Architecture

## Current boundary

```text
Official FPL API
      |
      v
Provider Adapter
      |
      v
Raw Record Store ----> Ingestion Run Audit
      |
      v
Normalization
      |
      v
PostgreSQL Canonical Domain
      |
      v
Future: Feature Store -> Models -> Predictions -> Optimization -> AI Analyst
```

## Design rules

1. Provider-specific IDs never replace internal IDs.
2. Raw payloads are retained with hashes and retrieval timestamps.
3. Historical values are stored as snapshots instead of overwritten facts where future decisions depend on them.
4. Season-specific FPL rules are versioned.
5. The AI layer consumes structured application tools rather than becoming the source of truth.
6. Backtesting must use information available before the historical cutoff.
