"""End-to-end dry-run evaluation for point-in-time availability.

Materializes deadline-adjacent fplcache snapshots, runs chronological
eligibility checks, and measures offline signal lift against actual minutes
when DATABASE_URL is available.

Never writes to the database. Safe for CI.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from fpl_intelligence.availability.historical.chronological import (
    evaluate_materialize_report,
)
from fpl_intelligence.availability.historical.materialize_pit import (
    DeadlineCutoff,
    materialize_cutoffs,
)
from fpl_intelligence.availability.historical.signal_lift import evaluate_signal_lift


def _parse_cutoff(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", action="append", required=True)
    parser.add_argument("--season", action="append", required=True)
    parser.add_argument("--gameweek", action="append", default=[])
    parser.add_argument("--cache-root", type=Path, default=Path("data/fplcache_pit"))
    parser.add_argument("--search-days", type=int, default=3)
    args = parser.parse_args(argv)

    if len(args.season) != len(args.cutoff):
        parser.error("one --season per --cutoff")
    if args.gameweek and len(args.gameweek) != len(args.cutoff):
        parser.error("one --gameweek per --cutoff when provided")
    gameweeks = (
        [int(g) for g in args.gameweek]
        if args.gameweek
        else [None] * len(args.cutoff)
    )

    cutoffs = [
        DeadlineCutoff(
            season_code=season,
            gameweek=gw,
            cutoff=_parse_cutoff(cutoff),
        )
        for season, gw, cutoff in zip(args.season, gameweeks, args.cutoff, strict=True)
    ]

    report = materialize_cutoffs(args.cache_root, cutoffs, search_days=args.search_days)
    chronological = evaluate_materialize_report(report)

    db = None
    try:
        from fpl_intelligence.db.session import validation_session_factory

        db = validation_session_factory()()
        lift = evaluate_signal_lift(report, db)
    except Exception as exc:  # noqa: BLE001
        lift = evaluate_signal_lift(report, None)
        lift.notes.append(f"DB link skipped: {type(exc).__name__}: {exc}")
    finally:
        if db is not None:
            db.close()

    payload = {
        "read_only": True,
        "materialize": report.to_dict(),
        "chronological": chronological.to_dict(),
        "signal_lift": lift.to_dict(),
        "gates": {
            "snapshots_found": report.missing == 0,
            "all_events_chronologically_eligible": chronological.ineligible == 0
            and chronological.total_events > 0,
            "signal_direction_ok": lift.to_dict().get("signal_direction_ok"),
        },
    }
    print(json.dumps(payload, indent=2, default=str))

    if report.missing > 0:
        return 2
    if chronological.total_events > 0 and chronological.ineligible > 0:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
