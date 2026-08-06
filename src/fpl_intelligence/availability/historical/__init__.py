"""Phase 7.2 — Historical Availability Data Acquisition and Integration.

Provides provider adapters that normalise external historical availability
sources (FPL bootstrap news, Transfermarkt-derived injury data, public injury
datasets, press-conference archives) into the canonical Phase 7 entities, with
honest temporal classification.

The critical no-look-ahead distinction is preserved: an event is
STRICT_BACKTEST_SAFE only when its information was available before the decision
cutoff (publication/availability timing), never merely because the event
occurred before the deadline. Events without sufficient temporal evidence are
imported, preserved, and marked HISTORICAL_EVENT_ONLY / UNKNOWN — never silently
accepted as strict pre-deadline intelligence.

The deterministic :class:`SampleHistoricalAvailabilityProvider` is clearly
labelled MOCK / ENGINEERING VERIFICATION ONLY and is excluded from real coverage
metrics and empirical Phase 7 classification.
"""
