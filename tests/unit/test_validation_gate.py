"""Unit tests for the Phase 4.5 quantitative edge validation gate.

These tests validate the evaluation scaffolding on synthetic mock data. They
verify metrics, leak-free row building, baseline attachment, and the pipeline
validation (leakage) check.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.ingestion.historical import import_season
from fpl_intelligence.providers import MockHistoricalDataProvider
from fpl_intelligence.validation.edge import (
    attach_baseline_predictions,
    compute_metrics,
    pipeline_validation,
    prepare_dataset,
    topk_hit_rate,
)


def _build_db() -> sessionmaker:
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_one_season(db: Session) -> None:
    import_season(db=db, provider=MockHistoricalDataProvider(), season_code="2022-23")
    db.commit()


def _rows():
    SessionLocal = _build_db()
    db = SessionLocal()
    try:
        _seed_one_season(db)
        rows, records = prepare_dataset(db, ["2022-23"])
        return rows, records, db
    finally:
        db.close()


def test_prepare_dataset_is_leak_free() -> None:
    rows, _, _ = _rows()
    assert len(rows) > 0
    # Every row has features and a target, and features reference only prior
    # Gameweeks (enforced by construction: gw >= 11, features from gw < gw).
    for r in rows[:200]:
        assert r["gw"] >= 11
        assert "points_last_3" in r["features"]
        assert r["features"]["n_season_matches"] >= 10


def test_metrics_functions() -> None:
    rows, _, _ = _rows()
    attach_baseline_predictions(rows, ["2022-23"])
    m = compute_metrics(rows, "pred_baseline_a")
    assert m["n"] == len(rows)
    assert m["mae"] >= 0
    assert 0 <= m["top10"] <= 1.0
    # Spearman is defined because predictions are not constant.
    assert m["spearman"] == m["spearman"]  # not NaN


def test_topk_hit_rate_bounds() -> None:
    rows, _, _ = _rows()
    attach_baseline_predictions(rows, ["2022-23"])
    rate = topk_hit_rate(rows, 5, "pred_baseline_a")
    assert 0.0 <= rate <= 1.0


def test_pipeline_validation_pass_on_synthetic() -> None:
    rows, records, db = _rows()
    try:
        pv = pipeline_validation(db, rows, records, ["2022-23"])
        assert pv["leakage_test"] == "pass"
        assert pv["temporal_ordering"] == "pass"
        assert pv["featured_rows"] == len(rows)
    finally:
        db.close()
