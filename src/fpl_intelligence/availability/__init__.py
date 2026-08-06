"""Phase 7 — News, Injury, and Availability Intelligence.

Pipeline:
    NEWS / AVAILABILITY → EVIDENCE → EVENT → CONFIDENCE → AVAILABILITY STATE
    → MINUTES MODEL → PLAYER PREDICTION → DISTRIBUTION → DECISION ENGINE

This module provides the data model, provider abstractions, evidence
corroboration engine, availability-state derivation, minutes-model integration,
prediction-provider wrapper, and evaluation framework for availability-aware
FPL intelligence.
"""
