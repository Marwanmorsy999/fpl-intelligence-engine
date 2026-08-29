"""Read-only end-to-end PIT evaluation.

This command never writes to the database. By default it proves snapshot,
event, and chronology safety; ``--require-signal`` additionally requires a
measured validation-DB signal rather than accepting structural counts.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from fpl_intelligence.availability.historical.chronological import evaluate_materialize_report
from fpl_intelligence.availability.historical.materialize_pit import DeadlineCutoff, materialize_cutoffs
from fpl_intelligence.availability.historical.signal_lift import evaluate_signal_lift


def _parse_cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", action="append", required=True)
    parser.add_argument("--season", action="append", required=True)
    parser.add_argument("--gameweek", action="append", default=[])
    parser.add_argument("--cache-root", type=Path, default=Path("data/fplcache_pit"))
    parser.add_argument("--search-days", type=int, default=3)
    parser.add_argument("--require-signal", action="store_true")
    args = parser.parse_args(argv)

    if len(args.season) != len(args.cutoff):
        parser.error("provide exactly one --season per --cutoff")
    if args.gameweek and len(args.gameweek) != len(args.cutoff):
        parser.error("provide exactly one --gameweek per --cutoff")
    gameweeks = [int(value) for value in args.gameweek] if args.gameweek else [None] * len(args.cutoff)
    cutoffs = [
        DeadlineCutoff(season, gw, _parse_cutoff(cutoff))
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
        lift.notes.append(f"DB link unavailable: {type(exc).__name__}: {exc}")
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
            "events_present": report.event_count > 0,
            "all_events_chronologically_eligible": report.event_count > 0
            and chronological.ineligible == 0
            and chronological.missing_timestamp == 0,
            "signal_direction_ok": lift.to_dict().get("signal_direction_ok", False),
        },
    }
    print(json.dumps(payload, indent=2, default=str))

    if report.missing:
        return 2
    if report.event_count == 0:
        return 3
    if chronological.ineligible or chronological.missing_timestamp:
        return 4
    if args.require_signal and not lift.to_dict().get("signal_direction_ok", False):
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
