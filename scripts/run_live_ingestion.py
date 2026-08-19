#!/usr/bin/env python
"""scripts/run_live_ingestion.py — Phase 9.5 Live Source Ingestion CLI.

Fetches news from live connectors (RSS feeds and the official FPL API) using
the :class:`ConnectorScheduler` and passes each fetched item through the
Phase 9.2 ingestion pipeline (``ingest_raw_text``), then prints an ingestion
summary.

Usage
-----
    python scripts/run_live_ingestion.py --connector all
    python scripts/run_live_ingestion.py --connector rss --rss-url https://...
    python scripts/run_live_ingestion.py --connector fpl_api
    python scripts/run_live_ingestion.py --connector all --dry-run
    python scripts/run_live_ingestion.py --connector all \\
        --interval 60 --iterations 5 --db ./fpl.db

``--dry-run`` still *fetches* from the network sources but **does not persist**
anything (the ingestion pipeline rolls its session back), so it is safe to
inspect what live ingestion would produce. ``--interval`` enables scheduled
execution; ``--iterations`` bounds how many passes it performs. By default an
in-memory SQLite database is used, so running without ``--db`` never touches
any real database.

Exit codes: ``0`` success, ``1`` usage/configuration error, ``2`` provider/
network error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(_SRC))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from fpl_intelligence.db.base import Base  # noqa: E402
from fpl_intelligence.live_intelligence.connectors import (  # noqa: E402
    ConnectorScheduler,
    FPLAPIConnector,
    RSSConnector,
    SchedulerReport,
    SourceConnector,
)
from fpl_intelligence.live_intelligence.raw_item_ledger import (  # noqa: E402
    RawItem,
    ingest_raw_text,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PROVIDER = 2

DEFAULT_RSS_URL = "https://www.bbc.co.uk/sport/football/teams/rss"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 9.5 — fetch live news via connectors and ingest it into the "
            "Phase 9.2 pipeline."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--connector",
        choices=["rss", "fpl_api", "all"],
        default="all",
        help="Which connector(s) to run (default: all).",
    )
    parser.add_argument(
        "--rss-url",
        default=None,
        help=f"RSS feed URL for the rss connector (default: {DEFAULT_RSS_URL}).",
    )
    parser.add_argument(
        "--source-id",
        default=None,
        help="Phase 9.2 source identifier override (defaults to connector default).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch from the live sources but do not persist anything.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Run repeatedly every N seconds (scheduled execution).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of scheduled passes (with --interval).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite DB path. Defaults to an in-memory database.",
    )
    return parser.parse_args(argv)


def _build_session(db_path: Path | None) -> Any:
    if db_path is None:
        engine = create_engine("sqlite:///:memory:", echo=False)
    else:
        engine = create_engine(f"sqlite:///{db_path}", echo=False)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def build_connectors(args: argparse.Namespace) -> dict[str, SourceConnector]:
    """Instantiate the real connectors selected by ``--connector``."""
    connectors: dict[str, SourceConnector] = {}

    if args.connector in ("all", "rss"):
        rss = RSSConnector(
            args.rss_url or DEFAULT_RSS_URL,
            source_id=args.source_id or "rss",
        )
        connectors[rss.name] = rss

    if args.connector in ("all", "fpl_api"):
        fpl = FPLAPIConnector()
        if args.source_id:
            fpl.source_id = args.source_id
        connectors[fpl.name] = fpl

    return connectors


def _print_report(report: SchedulerReport, *, dry_run: bool) -> None:
    print("=" * 78)
    print("PHASE 9.5 — LIVE SOURCE INGESTION SUMMARY")
    print("=" * 78)
    for name, stats in report.runs.items():
        print(f"  connector      : {name}")
        print(f"    fetched      : {stats.fetched}")
        print(f"    ingested     : {stats.ingested}")
        for err in stats.errors:
            print(f"    error        : {err}")
    print(f"  connectors ran : {report.connectors_ran}")
    print(f"  total fetched  : {report.total_fetched}")
    print(f"  total ingested : {report.total_ingested}")
    print(f"  total errors   : {report.total_errors}")
    if dry_run:
        print("  dry_run        : True (fetched but nothing persisted)")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.interval is not None and args.interval <= 0:
        print("USAGE ERROR: --interval must be positive.")
        return EXIT_USAGE
    if args.iterations < 1:
        print("USAGE ERROR: --iterations must be at least 1.")
        return EXIT_USAGE

    SessionLocal = _build_session(args.db)
    db = SessionLocal()
    scheduler = None
    try:
        connectors = build_connectors(args)

        def sink(raw: RawItem, *, connector: SourceConnector, dry_run: bool) -> Any:
            return ingest_raw_text(
                db,
                source_id=raw.source_id,
                text=raw.content_text,
                published_at=raw.published_at,
                url=raw.url,
                title=raw.title,
                external_id=raw.external_id,
                source_type=connector.source_type,
                dry_run=dry_run,
            )

        scheduler = ConnectorScheduler(connectors, sink)
        connector_flag = None if args.connector == "all" else args.connector

        if args.interval is not None:
            reports = scheduler.run_scheduled(
                interval_seconds=args.interval,
                iterations=args.iterations,
                connector=connector_flag,
                dry_run=args.dry_run,
            )
            for i, report in enumerate(reports, start=1):
                print(f"\n--- pass {i}/{len(reports)} ---")
                _print_report(report, dry_run=args.dry_run)
        else:
            report = scheduler.run(connector=connector_flag, dry_run=args.dry_run)
            _print_report(report, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - report network/provider errors cleanly
        print(f"PROVIDER ERROR: {exc}")
        return EXIT_PROVIDER
    finally:
        if scheduler is not None:
            for conn in scheduler.connectors.values():
                conn.close()
        db.close()

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())