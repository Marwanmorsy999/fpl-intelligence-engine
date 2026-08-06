"""Phase 4 — Prediction Engine + Baseline Models.

This package provides the first quantitative prediction layer for the
FPL Intelligence Engine. It contains:

- ``base`` — PredictionModel protocol
- ``models`` — SQLAlchemy ORM models for predictions, registry, team strengths
- ``training`` — Temporal TrainingDataBuilder (no target leakage)
- ``baselines`` — Baseline A (recent form), B (minutes-adjusted), C (fixture-adjusted)
- ``minutes`` — MinutesModel (P(start), P(30+), P(60+), expected minutes)
- ``team`` — TeamStrengthModel (attack/defence/home/away)
- ``match`` — PoissonMatchModel
- ``simulation`` — MatchSimulator (Monte Carlo)
- ``scoring`` — FPLScoringEngine (rules-versioned)
- ``pipeline`` — PlayerBaselinePipeline (connects all components)
- ``registry`` — ModelRegistry (save/load/promote/retire)
- ``persistence`` — PredictionPersistence (immutable records)
- ``data_quality`` — Data quality assessment
- ``evaluation`` — Calibration, model comparison, context breakdown
- ``walkforward`` — Walk-forward training/evaluation
"""

from __future__ import annotations
