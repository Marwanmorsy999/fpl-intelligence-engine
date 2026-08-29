"""Materialize/evaluate PIT availability with explicit dry-run defaults.

No production connection is used here. --import targets the existing validation
session factory and --commit is refused unless chronology/entity gates pass.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from fpl_intelligence.availability.historical.chronological import evaluate_materialize_report
from fpl_intelligence.availability.historical.materialize_pit import DeadlineCutoff, collect_events, import_materialized, materialize_cutoffs
from fpl_intelligence.availability.historical.signal_lift import evaluate_signal_lift

# Known validation Supabase project. A commit is rejected unless DATABASE_URL
# clearly identifies this project (direct host or the project-qualified pooler user).
VALIDATION_PROJECT_REF = "hnsoektotpqgvpqshusi"


def _assert_validation_database_target() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("--commit requires DATABASE_URL for the validation database")
    if VALIDATION_PROJECT_REF not in url:
        raise SystemExit(
            "refusing --commit: DATABASE_URL does not identify the approved validation Supabase project"
        )


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _cutoffs_from_args(args: argparse.Namespace) -> list[DeadlineCutoff]:
    if args.from_verified_deadlines:
        from fpl_intelligence.availability.historical.verified_deadlines import load_verified_deadline_cutoffs
        return load_verified_deadline_cutoffs(
            args.season_code or None,
            gw_min=args.gw_min,
            gw_max=args.gw_max,
            limit=args.limit,
        )
    if args.from_db_deadlines:
        if not args.season_code:
            raise SystemExit("--from-db-deadlines requires --season-code")
        from fpl_intelligence.availability.historical.deadlines import load_deadline_cutoffs
        from fpl_intelligence.db.session import validation_session_factory
        Session = validation_session_factory()
        with Session() as db:
            return load_deadline_cutoffs(db, args.season_code, gw_min=args.gw_min, gw_max=args.gw_max, limit=args.limit)
    if not args.cutoff or not args.season:
        raise SystemExit("provide --cutoff/--season pairs, --from-verified-deadlines, or --from-db-deadlines")
    if len(args.cutoff) != len(args.season):
        raise SystemExit("provide exactly one --season per --cutoff")
    gameweeks = [int(g) for g in args.gameweek] if args.gameweek else [None] * len(args.cutoff)
    if len(gameweeks) != len(args.cutoff):
        raise SystemExit("provide exactly one --gameweek per --cutoff")
    return [DeadlineCutoff(season, gw, _dt(cutoff)) for season, gw, cutoff in zip(args.season, gameweeks, args.cutoff, strict=True)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", action="append", default=[])
    parser.add_argument("--season", action="append", default=[])
    parser.add_argument("--gameweek", action="append", default=[])
    parser.add_argument("--from-db-deadlines", action="store_true")
    parser.add_argument("--from-verified-deadlines", action="store_true")
    parser.add_argument("--season-code", action="append", default=[])
    parser.add_argument("--gw-min", type=int, default=None)
    parser.add_argument("--gw-max", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--cache-root", type=Path, default=Path("data/fplcache_pit"))
    parser.add_argument("--search-days", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--import", dest="do_import", action="store_true")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)

    if args.from_db_deadlines and args.from_verified_deadlines:
        raise SystemExit("--from-db-deadlines and --from-verified-deadlines are mutually exclusive")
    if args.commit:
        _assert_validation_database_target()
    cutoffs = _cutoffs_from_args(args)
    if not cutoffs:
        print(json.dumps({"error": "no deadline cutoffs available"}, indent=2))
        return 2

    report = materialize_cutoffs(args.cache_root, cutoffs, search_days=args.search_days, force=args.force)
    chronology = evaluate_materialize_report(report)
    db = None
    lift = evaluate_signal_lift(report, None)
    if args.evaluate or args.do_import:
        try:
            from fpl_intelligence.db.session import validation_session_factory
            db = validation_session_factory()()
            lift = evaluate_signal_lift(report, db)
        except Exception as exc:  # noqa: BLE001
            lift.notes.append(f"DB link unavailable: {type(exc).__name__}: {exc}")
        finally:
            if db is not None and not args.do_import:
                db.close()
                db = None

    payload = {
        "dry_run": not args.do_import,
        "materialize": report.to_dict(),
        "chronological": chronology.to_dict(),
        "signal_lift": lift.to_dict(),
        "sample_events": collect_events(report)[:5],
        "gates": {
            "snapshots_found": report.missing == 0,
            "events_present": report.event_count > 0,
            "chronology_ok": report.event_count > 0 and chronology.ineligible == 0 and chronology.missing_timestamp == 0,
            "signal_direction_ok": lift.to_dict().get("signal_direction_ok", False),
        },
    }

    if args.do_import:
        if db is None:
            raise SystemExit("validation DB session is required for --import")
        import_result = import_materialized(db, report, strict_backtest_safe=True)
        payload["import_result"] = import_result
        audit = import_result.get("resolver_audit", {})
        resolution_ok = (audit.get("ambiguous", 0) or 0) == 0 and (audit.get("unmatched", 0) or 0) == 0
        gates_ok = (
            payload["gates"]["snapshots_found"]
            and payload["gates"]["events_present"]
            and payload["gates"]["chronology_ok"]
            and resolution_ok
            and payload["gates"]["signal_direction_ok"]
        )
        payload["gates"]["entity_resolution_ok"] = resolution_ok
        payload["gates"]["all_import_gates"] = gates_ok
        if args.commit and not gates_ok:
            db.rollback()
            payload["committed"] = False
            print(json.dumps(payload, indent=2, default=str))
            return 5
        if args.commit:
            db.commit()
            payload["committed"] = True
        else:
            db.rollback()
            payload["committed"] = False

    print(json.dumps(payload, indent=2, default=str))
    return 0 if report.missing == 0 and report.event_count > 0 and chronology.ineligible == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main())
