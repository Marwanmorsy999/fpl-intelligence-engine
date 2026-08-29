"""Materialize point-in-time FPL availability from Randdalf/fplcache.

Default mode is dry-run: download deadline-adjacent snapshots, parse flagged
players, and print a JSON report. Pass --import to persist into Phase 7 tables
(still isolated to the feature/availability-pit development path).

Examples:
  python scripts/materialize_availability_pit.py \\
    --cutoff 2024-08-16T16:00:00Z --season 2024-25 --gameweek 1 \\
    --cutoff 2025-08-15T16:00:00Z --season 2025-26 --gameweek 1

  python scripts/materialize_availability_pit.py --import ...
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from fpl_intelligence.availability.historical.materialize_pit import (
    DeadlineCutoff,
    collect_events,
    import_materialized,
    materialize_cutoffs,
)


def _parse_cutoff(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cutoff",
        action="append",
        required=True,
        help="UTC ISO-8601 deadline cutoff (repeatable)",
    )
    parser.add_argument(
        "--season",
        action="append",
        required=True,
        help="Season code aligned positionally with each --cutoff (e.g. 2024-25)",
    )
    parser.add_argument(
        "--gameweek",
        action="append",
        default=[],
        help="Optional gameweek number aligned with each --cutoff",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path("data/fplcache_pit"),
        help="Local directory for materialized snapshots",
    )
    parser.add_argument(
        "--search-days",
        type=int,
        default=3,
        help="Days to search backward for a snapshot",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if local snapshot exists",
    )
    parser.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="Persist events into Phase 7 tables (requires DATABASE_URL)",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Commit the import transaction (default rolls back unless set)",
    )
    args = parser.parse_args(argv)

    if len(args.season) != len(args.cutoff):
        parser.error("provide exactly one --season per --cutoff")
    gameweeks: list[int | None]
    if not args.gameweek:
        gameweeks = [None] * len(args.cutoff)
    elif len(args.gameweek) != len(args.cutoff):
        parser.error("when using --gameweek, provide one per --cutoff")
    else:
        gameweeks = [int(g) for g in args.gameweek]

    cutoffs = [
        DeadlineCutoff(
            season_code=season,
            gameweek=gw,
            cutoff=_parse_cutoff(cutoff),
        )
        for season, gw, cutoff in zip(args.season, gameweeks, args.cutoff, strict=True)
    ]

    report = materialize_cutoffs(
        args.cache_root,
        cutoffs,
        search_days=args.search_days,
        force=args.force,
    )

    if args.do_import:
        from fpl_intelligence.db.session import writable_validation_session_factory

        Session = writable_validation_session_factory()
        with Session() as db:
            import_materialized(db, report, strict_backtest_safe=True)
            if args.commit:
                db.commit()
            else:
                db.rollback()
                if report.import_result is not None:
                    report.import_result["committed"] = False
                    report.import_result["note"] = (
                        "import exercised but rolled back; pass --commit to persist"
                    )

    # Keep event bodies out of the default stdout report for size; counts only.
    payload = report.to_dict()
    payload["sample_events"] = collect_events(report)[:5]
    print(json.dumps(payload, indent=2, default=str))
    return 0 if report.missing == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
