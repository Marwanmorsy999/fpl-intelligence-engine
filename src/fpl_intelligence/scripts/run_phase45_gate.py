"""Phase 4.5 Quantitative Edge Validation Gate -- runner.

Builds an in-memory database, loads the (mock/synthetic) historical seasons,
runs data audit + all evaluation steps, and writes the benchmark report.

Usage:
    python -m fpl_intelligence.scripts.run_phase45_gate
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.ingestion.historical import import_season
from fpl_intelligence.providers import MockHistoricalDataProvider
from fpl_intelligence.validation.data_audit import audit_data_coverage
from fpl_intelligence.validation.edge import run_full_gate
from fpl_intelligence.validation.report import write_report

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("phase45")

PRIMARY_SEASONS = ["2022-23", "2023-24", "2024-25"]
ALL_SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26", "2026-27"]


def build_db() -> sessionmaker:
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def main() -> None:
    t0 = time.time()
    SessionLocal = build_db()
    db = SessionLocal()
    try:
        provider = MockHistoricalDataProvider()
        print("Loading mock (synthetic) historical seasons ...")
        for season in ALL_SEASONS:
            import_season(db=db, provider=provider, season_code=season)
            db.commit()
            print(f"  loaded {season}")

        print("\nData coverage audit:")
        try:
            audit = audit_data_coverage(db)
            print(f"  seasons available: {audit.seasons_available}")
            print(f"  eligible seasons:  {audit.eligible_seasons}")
        except Exception as exc:  # noqa: BLE001
            print(f"  data audit failed: {exc}")

        print(f"\nRunning full Phase 4.5 gate on {PRIMARY_SEASONS} ...")
        results = run_full_gate(db, PRIMARY_SEASONS)

        path = write_report(results)
        print(f"\nWrote report to: {path}")
        print(f"Total rows (featured player-gameweeks): {results.get('rows_built')}")
        print(
            f"Pipeline validation: {results['pipeline_validation']['leakage_test']} "
            f"(temporal ordering {results['pipeline_validation']['temporal_ordering']})"
        )
        print(f"Elapsed: {time.time() - t0:.1f}s")
    finally:
        db.close()


if __name__ == "__main__":
    main()
