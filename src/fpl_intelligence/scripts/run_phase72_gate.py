"""Phase 7.2 evaluation-readiness gate.

Computes ``strict_safe_event_coverage`` per development season and reports the
phase 7 empirical-readiness honestly:

- If no real strict-backtest-safe availability data is wired in, the phase is
  classified PARTIALLY_TESTABLE / BLOCKED (never fabricated as A/B/C).
- The locked 2025-26 holdout is excluded from strict-safe coverage and from any
  tuning; it is only reported separately.

This gate does NOT run BASELINE vs PHASE7. It only reports whether the
availability dataset is sufficiently populated and temporally valid to make a
real empirical evaluation meaningful.

The gate reads the authoritative coverage from the persisted Phase 7.2 import
result (``docs/phase7-2-import-result.json``), which is produced by
``run_phase72_import`` after the full normalize -> resolve -> classify ->
persist pipeline. This keeps the gate reproducible and honest: it never audits
an empty in-memory DB as if it represented the real imported dataset.

Usage:
    python -m fpl_intelligence.scripts.run_phase72_gate [--seasons ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from fpl_intelligence.db.base import Base

DOCS = Path(__file__).resolve().parents[3] / "docs"
DEFAULT_SEASONS = ["2022-23", "2023-24", "2024-25"]
HOLDOUT = "2025-26"

#: Minimum strict-safe coverage per season below which we cannot meaningfully
#: evaluate the Phase 7 models. This is an explicit, honest threshold (not a
#: fabricated result).
MIN_STRICT_SAFE_COVERAGE_PCT = 10.0


def build_db() -> sessionmaker:
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # Import the Phase 7 availability models so their tables are registered on
    # Base.metadata BEFORE create_all runs. Without this, the availability
    # tables are never created and the audit queries fail with
    # "no such table: availability_sources".
    from fpl_intelligence import availability  # noqa: F401
    from fpl_intelligence.availability import models  # noqa: F401

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _load_import_coverage() -> dict[str, Any]:
    """Load the authoritative coverage dict from the persisted import result.

    Returns an empty structure if the import result file is absent so the gate
    degrades to a BLOCKED verdict rather than fabricating data.
    """
    import_result_path = DOCS / "phase7-2-import-result.json"
    if not import_result_path.exists():
        return {"coverage": {}}
    try:
        return json.loads(import_result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"coverage": {}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 7.2 evaluation-readiness gate.")
    parser.add_argument(
        "--seasons",
        nargs="*",
        default=DEFAULT_SEASONS,
        help="Development seasons to assess (default 2022-23..2024-25).",
    )
    args = parser.parse_args()

    t0 = time.time()
    db = build_db()()

    # Load the authoritative coverage from the persisted real import result.
    data = _load_import_coverage()
    cov = data.get("coverage", {})
    strict_map: dict[str, float] = cov.get("strict_safe_event_coverage", {}) or {}
    per_season_coverage: dict[str, dict[str, Any]] = cov.get("season_coverage", {}) or {}

    # Determine readiness per development season from the persisted import.
    per_season: dict[str, dict[str, Any]] = {}
    for code in args.seasons:
        cov_row = per_season_coverage.get(code, {})
        strict_pct = float(strict_map.get(code, 0.0))
        total = int(cov_row.get("total_events", 0))
        strict_safe_events = int(cov_row.get("strict_safe_events", 0))
        per_season[code] = {
            "total_events": total,
            "strict_safe_events": strict_safe_events,
            "strict_safe_event_coverage_pct": strict_pct,
            "sufficient": total > 0 and strict_pct >= MIN_STRICT_SAFE_COVERAGE_PCT,
        }

    sufficient = [c for c in per_season.values() if c["sufficient"]]
    any_real_events = any(c["total_events"] > 0 for c in per_season.values())

    if sufficient and any_real_events:
        phase7_empirical = "PARTIALLY TESTABLE"
        classification = "PARTIALLY_TESTABLE"
        reason = (
            "Real strict-backtest-safe availability events are present in the "
            "development seasons above the minimum coverage threshold. A "
            "BASELINE vs PHASE7 empirical comparison may be meaningful, subject "
            "to the full evaluation (metrics, decision ROI, holdout isolation)."
        )
    else:
        phase7_empirical = "BLOCKED / PARTIALLY TESTABLE"
        classification = "BLOCKED_OR_PARTIALLY_TESTABLE"
        reason = (
            "Insufficient strict-backtest-safe historical availability data. "
            "Phase 7 empirical value is NOT fabricated as A/B/C. The "
            "availability dataset does not yet support a meaningful real "
            "BASELINE vs PHASE7 comparison."
        )

    result = {
        "phase7_empirical": phase7_empirical,
        "classification": classification,
        "reason": reason,
        "min_strict_safe_coverage_pct": MIN_STRICT_SAFE_COVERAGE_PCT,
        "development_seasons": per_season,
        "holdout": HOLDOUT,
        "holdout_note": (
            "The locked 2025-26 holdout is isolated and NOT used for tuning or "
            "strict-safe coverage assessment."
        ),
        "coverage_source": "docs/phase7-2-import-result.json (persisted real import)",
        "coverage": cov,
        "elapsed_s": round(time.time() - t0, 1),
    }

    print("\n=== Phase 7.2 Evaluation-Readiness Gate ===")
    print(f"Phase 7 empirical: {phase7_empirical}")
    print(f"Classification: {classification}")
    print(f"Reason: {reason}")
    print("\nPer-season strict-safe coverage:")
    for code, row in per_season.items():
        print(
            f"  {code}: {row['strict_safe_event_coverage_pct']}% "
            f"({row['strict_safe_events']}/{row['total_events']} events, "
            f"sufficient={row['sufficient']})"
        )

    out = DOCS / "phase7-2-evaluation-gate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
