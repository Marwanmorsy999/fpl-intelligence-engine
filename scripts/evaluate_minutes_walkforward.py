"""Generate the canonical Stage 2A minutes walk-forward report."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from fpl_intelligence.config.holdout import DEVELOPMENT_SEASONS
from fpl_intelligence.db.session import validation_session_factory
from fpl_intelligence.prediction.minutes_validation import (
    FEATURE_VERSION,
    ValidationResult,
    render_report,
)
from fpl_intelligence.prediction.minutes_validation_fast import FastMinutesWalkForwardEvaluator


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", nargs="+", default=DEVELOPMENT_SEASONS)
    parser.add_argument("--initial-train-folds", type=int, default=3)
    parser.add_argument("--report", type=Path, default=Path("docs/STAGE_2A_MINUTES_VALIDATION.md"))
    args = parser.parse_args()

    try:
        session_factory = validation_session_factory()
        with session_factory() as db:
            result = FastMinutesWalkForwardEvaluator(
                db, feature_version=FEATURE_VERSION
            ).run(args.seasons, initial_train_folds=args.initial_train_folds)
    except (RuntimeError, SQLAlchemyError) as exc:
        message = str(exc).splitlines()[0] if isinstance(exc, RuntimeError) else (
            f"database access failed: {type(exc).__name__}"
        )
        result = ValidationResult(
            rows=[],
            folds=[],
            exclusions={"no_temporal_provenance": 0, "insufficient_training_rows": 0},
            seasons=sorted(args.seasons),
            data_error=message,
        )
        print(f"Canonical database unavailable; wrote an insufficient-evidence report: {message}")
        exit_code = 1
    else:
        exit_code = 0

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(result), encoding="utf-8")
    print(f"Wrote {args.report} (N={len(result.rows)})")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
