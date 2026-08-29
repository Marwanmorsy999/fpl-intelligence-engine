"""Stage 2A.7 historical backfill driver (canonical production database).

Modes
-----

Dry run (no writes to canonical tables; safe to run anytime)::

    python scripts/backfill_historical_seasons.py --dry-run
    python scripts/backfill_historical_seasons.py --dry-run --season 2022-23

Sequential import (deliberate, idempotent, through the existing
``import_season`` pipeline; run one season at a time)::

    python scripts/backfill_historical_seasons.py --import --season 2022-23

Read-only post-import verification::

    python scripts/backfill_historical_seasons.py --verify
    python scripts/backfill_historical_seasons.py --verify --season 2023-24

Safety properties:

* Never drops, truncates, deletes, or rewrites any existing row.
* Only ever inserts rows for the requested missing historical season via
  ``fpl_intelligence.ingestion.historical.import_season`` (idempotent:
  re-imports are no-ops keyed on IngestionRun / natural keys).
* Requires an explicitly configured PostgreSQL ``DATABASE_URL`` (the same
  fail-closed path as the Stage 2A validation scripts); never localhost.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from fpl_intelligence.db.models import (  # noqa: E402
    Fixture,
    FPLSnapshot,
    Gameweek,
    IngestionRun,
    PlayerExternalId,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
    RawRecord,
    Season,
    TeamExternalId,
)
from fpl_intelligence.db.session import validation_session_factory  # noqa: E402
from fpl_intelligence.ingestion.historical import import_season  # noqa: E402
from fpl_intelligence.providers.real_fpl import RealFPLProvider  # noqa: E402

TARGET_SEASONS = ("2022-23", "2023-24", "2024-25")


def _season_rows(db, model, season_id: int) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(model).where(model.season_id == season_id)
        )
        or 0
    )


def _canonical_counts(db, season_code: str) -> dict[str, object]:
    season = db.scalar(select(Season).where(Season.code == season_code))
    if season is None:
        return {"season_found": False}
    fixtures = _season_rows(db, Fixture, season.id)
    gameweeks = _season_rows(db, Gameweek, season.id)
    pgp = _season_rows(db, PlayerGameweekPerformance, season.id)
    snapshots = _season_rows(db, FPLSnapshot, season.id)
    memberships = _season_rows(db, PlayerTeamMembership, season.id)
    teams = int(
        db.scalar(
            select(func.count())
            .select_from(TeamExternalId)
            .where(TeamExternalId.provider == "real_fpl")
        )
        or 0
    )
    players = int(
        db.scalar(
            select(func.count())
            .select_from(PlayerExternalId)
            .where(PlayerExternalId.provider == "real_fpl")
        )
        or 0
    )
    # OUTCOME_DATA_ONLY provenance coverage / invariant checks.
    stamped = int(
        db.scalar(
            select(func.count())
            .select_from(PlayerGameweekPerformance)
            .where(
                PlayerGameweekPerformance.season_id == season.id,
                PlayerGameweekPerformance.available_at.is_not(None),
                PlayerGameweekPerformance.ingested_at.is_not(None),
            )
        )
        or 0
    )
    bad_order = int(
        db.scalar(
            select(func.count())
            .select_from(PlayerGameweekPerformance)
            .where(
                PlayerGameweekPerformance.season_id == season.id,
                PlayerGameweekPerformance.available_at.is_not(None),
                PlayerGameweekPerformance.ingested_at.is_not(None),
                PlayerGameweekPerformance.available_at > PlayerGameweekPerformance.ingested_at,
            )
        )
        or 0
    )
    runs = list(
        db.execute(
            select(IngestionRun)
            .where(
                IngestionRun.source == "real_fpl",
                IngestionRun.season_code == season_code,
            )
            .order_by(IngestionRun.started_at)
        ).scalars()
    )
    raw_records = int(
        db.scalar(
            select(func.count())
            .select_from(RawRecord)
            .where(RawRecord.source == "real_fpl", RawRecord.season_code == season_code)
        )
        or 0
    )
    return {
        "season_found": True,
        "season_id": season.id,
        "teams_external": teams,
        "players_external": players,
        "fixtures": fixtures,
        "gameweeks": gameweeks,
        "player_gameweek_performances": pgp,
        "stamped_provenance": stamped,
        "invalid_available_gt_ingested": bad_order,
        "memberships": memberships,
        "fpl_snapshots": snapshots,
        "raw_records": raw_records,
        "ingestion_runs": [
            {
                "job": run.job_name,
                "status": run.status,
                "started_at": run.started_at.isoformat(),
                "records_processed": run.records_processed,
            }
            for run in runs
        ],
    }


def _dry_run(db, provider: RealFPLProvider, season_code: str) -> dict[str, object]:
    """Dry run via the existing pipeline (rolls back) plus in-memory counts."""
    report = import_season(db, provider, season_code, dataset="all", dry_run=True)

    # Expected canonical player-GW rows: distinct (player, gameweek) pairs the
    # provider would contribute after the pipeline's double-GW aggregation.
    history = provider.get_fpl_history(season_code)
    pairs = {
        (str(h.get("provider_player_id")), int(h["gameweek"]))
        for h in history
        if h.get("provider_player_id") and h.get("gameweek") is not None
    }
    fixtures = provider.get_fixtures(season_code)
    teams = provider.get_teams(season_code)
    players = provider.get_players(season_code)
    return {
        "season": season_code,
        "season_found": True,
        "teams": len(teams),
        "players": len(players),
        "fixtures": len(fixtures),
        "fpl_performance_rows": len(history),
        "expected_canonical_player_gw_rows": len(pairs),
        "reconciliation": {
            "received": report.records_received,
            "accepted": report.records_accepted,
            "rejected": report.records_rejected,
            "unmatched_teams": len(report.unmatched_teams),
            "unmatched_players": len(report.unmatched_players),
            "duplicate_candidates": len(report.duplicate_candidates),
            "warnings": len(report.warnings),
            "critical_errors": len(report.critical_errors),
            "critical_detail": [
                f"[{e.category}] {e.message}" for e in report.critical_errors[:10]
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Dry run without writing")
    group.add_argument("--import", dest="do_import", action="store_true", help="Run the import")
    group.add_argument("--verify", action="store_true", help="Read-only verification")
    parser.add_argument("--season", choices=TARGET_SEASONS, help="Restrict to one season")
    args = parser.parse_args()

    seasons = [args.season] if args.season else list(TARGET_SEASONS)
    provider = RealFPLProvider()

    try:
        session_factory = validation_session_factory()
    except RuntimeError as exc:
        print(f"refusing to run: {exc}")
        return 2

    try:
        with session_factory() as db:
            if args.dry_run:
                for season_code in seasons:
                    result = _dry_run(db, provider, season_code)
                    print(f"DRY RUN {season_code}: {result}")
                return 0

            if args.do_import:
                for season_code in seasons:
                    print(f"IMPORT {season_code}: starting (idempotent import_season)")
                    report = import_season(db, provider, season_code, dataset="all")
                    print(report.summary())
                    print(f"VERIFY {season_code}: {_canonical_counts(db, season_code)}")
                return 0

            # --verify
            for season_code in seasons:
                counts = _canonical_counts(db, season_code)
                print(f"VERIFY {season_code}: {counts}")
            return 0
    except (RuntimeError, SQLAlchemyError, ValueError) as exc:
        print(f"backfill failed: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())