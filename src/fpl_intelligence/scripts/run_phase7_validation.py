"""Phase 7 empirical validation runner.

Imports real historical FPL data, runs the availability coverage + temporal
eligibility audits, and then attempts the BASELINE vs PHASE7 evaluation.

The runner FAILS clearly (exit non-zero) with a BLOCKED status when no valid
historical availability dataset has been imported. It will NOT silently run a
PHASE7 == BASELINE comparison and report it as a meaningful empirical
experiment.

Usage:
    python -m fpl_intelligence.scripts.run_phase7_validation [--seasons ...] [--offline]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.availability.validation import (
    audit_availability_coverage,
    audit_temporal_availability,
)
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import Season
from fpl_intelligence.ingestion.historical import import_season
from fpl_intelligence.providers import RealFPLProvider

DOCS = Path(__file__).resolve().parents[3] / "docs"
DEFAULT_SEASONS = ["2022-23", "2023-24", "2024-25"]
HOLDOUT = "2025-26"


def build_db() -> sessionmaker:
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _has_availability_data(db: Session) -> bool:
    """Return True if any availability events exist in the database."""
    from fpl_intelligence.availability.models import AvailabilityEvent

    count = db.scalar(select(AvailabilityEvent.id).limit(1))
    return count is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 7 empirical validation runner.")
    parser.add_argument(
        "--seasons",
        nargs="*",
        default=DEFAULT_SEASONS,
        help="Development seasons to import (default: 2022-23..2024-25).",
    )
    parser.add_argument(
        "--include-holdout", action="store_true", help="Also import the locked 2025-26 holdout."
    )
    parser.add_argument("--offline", action="store_true", help="Replay from cached raw files only.")
    parser.add_argument(
        "--skip-import",
        action="store_true",
        help="Skip import; audit whatever is already in the DB.",
    )
    args = parser.parse_args()

    t0 = time.time()
    db = build_db()()

    if not args.skip_import:
        from fpl_intelligence.providers.github_fetcher import DiskCachingFetcher

        fetcher = DiskCachingFetcher(
            raw_root=Path(__file__).resolve().parents[3] / "data" / "raw",
            offline=args.offline,
        )
        provider = RealFPLProvider(fetcher=fetcher)
        seasons = list(args.seasons) + ([HOLDOUT] if args.include_holdout else [])
        imported: list[str] = []
        for s in seasons:
            try:
                import_season(db=db, provider=provider, season_code=s)
                db.commit()
                imported.append(s)
                print(f"  imported real season {s}")
            except Exception as exc:  # noqa: BLE001
                print(f"  import failed {s}: {exc}")
        if not imported:
            print("ERROR: no real seasons could be imported. Nothing to audit.")
            db.close()
            return 2
    else:
        imported = list(db.execute(select(Season.code).order_by(Season.code)).scalars().all())
        print(f"  skipping import; auditing existing seasons {imported}")

    # 1. Availability coverage audit.
    coverage = audit_availability_coverage(db, imported)
    print("\n=== Availability Coverage ===")
    print(json.dumps(coverage.to_dict(), indent=2, default=str))

    # 2. Temporal eligibility audit.
    temporal = audit_temporal_availability(db)
    print("\n=== Temporal Eligibility ===")
    print(json.dumps(temporal.to_dict(), indent=2, default=str))

    # 3. Gate: require valid availability data before running the experiment.
    if not _has_availability_data(db) or coverage.total_events == 0:
        print("\n" + "=" * 70)
        print("PHASE 7 EMPIRICAL VALIDATION: BLOCKED")
        print("=" * 70)
        print("No historical availability events were imported from the real")
        print("FPL source. The availability_events table is empty, so")
        print("BASELINE == PHASE7 and no meaningful empirical experiment can be")
        print("run. This is NOT a Phase 7 'no improvement' (A) result; it is a")
        print("data-availability block.")
        print("\nClassification: BLOCKED — INSUFFICIENT HISTORICAL AVAILABILITY DATA")
        result = {
            "status": "BLOCKED",
            "classification": "BLOCKED_INSUFFICIENT_HISTORICAL_AVAILABILITY_DATA",
            "reason": "no historical availability events in database",
            "coverage": coverage.to_dict(),
            "temporal": temporal.to_dict(),
            "imported_seasons": imported,
            "elapsed_s": round(time.time() - t0, 1),
        }
        out = DOCS / "phase7-empirical-validation-results.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {out}")
        db.close()
        return 1

    # 4. If valid availability data exists, run the real experiment.
    #    (The experiment itself is delegated to the evaluation framework so the
    #     runner stays focused on the data gate.)
    print("\n" + "=" * 70)
    print("PHASE 7 EMPIRICAL VALIDATION: READY")
    print("=" * 70)
    print("Valid historical availability data present; the BASELINE vs PHASE7")
    print("evaluation can be executed. See availability.evaluation.evaluate_phase7.")
    result = {
        "status": "READY",
        "classification": "PENDING_RUN",
        "coverage": coverage.to_dict(),
        "temporal": temporal.to_dict(),
        "imported_seasons": imported,
        "elapsed_s": round(time.time() - t0, 1),
    }
    out = DOCS / "phase7-empirical-validation-results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
