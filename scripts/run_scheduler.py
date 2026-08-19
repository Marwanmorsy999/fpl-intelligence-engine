#!/usr/bin/env python
"""scripts/run_scheduler.py — Phase 9.6 Scheduling & Alerting CLI.

Fetches news from live connectors (RSS feeds and the official FPL API) using
the Phase 9.6 :class:`Scheduler`, passes each fetched item through the Phase 9.2
ingestion pipeline (``ingest_raw_text``), generates alerts from the newly
ingested items, optionally notifies the user, and prints the summary.

Usage
-----
    python scripts/run_scheduler.py --connector all
    python scripts/run_scheduler.py --connector rss --rss-url https://...
    python scripts/run_scheduler.py --connector fpl_api
    python scripts/run_scheduler.py --connector all --dry-run
    python scripts/run_scheduler.py --connector all --notify slack \\
        --slack-webhook-url https://hooks.slack.com/...
    python scripts/run_scheduler.py --connector all --notify email \\
        --email-from alerts@example.com --email-to me@example.com \\
        --smtp-host smtp.example.com --smtp-port 587 \\
        --smtp-user me --smtp-password "***"
    python scripts/run_scheduler.py --connector all --interval 60 \\
        --iterations 5 --db ./fpl.db

``--dry-run`` still *fetches* from the live sources but **does not persist**
(the ingestion pipeline rolls back; ``--notify log`` still prints alerts).
``--interval`` enables scheduled execution; ``--iterations`` bounds how many
passes it performs. By default an in-memory SQLite database is used, so a bare
run never touches a real database.

Slack webhook and SMTP credentials are read from arguments or environment
variables (``SLACK_WEBHOOK_URL``, ``SMTP_HOST``, ``SMTP_USERNAME``,
``SMTP_PASSWORD``) — never hardcoded.

Exit codes: ``0`` success, ``1`` usage/configuration error, ``2`` provider/
network error.
"""
from __future__ import annotations

import argparse
import os
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
    FPLAPIConnector,
    RSSConnector,
    SourceConnector,
)
from fpl_intelligence.live_intelligence.raw_item_ledger import (  # noqa: E402
    RawItem,
    ingest_raw_text,
)
from fpl_intelligence.live_intelligence.scheduling import (  # noqa: E402
    AlertGenerator,
    EmailNotifier,
    LogNotifier,
    NotificationService,
    Notifier,
    Scheduler,
    SchedulerRunReport,
    SlackNotifier,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PROVIDER = 2

DEFAULT_RSS_URL = "https://www.bbc.co.uk/sport/football/teams/rss"

NOTIFY_CHOICES = ("none", "log", "slack", "email")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 9.6 — fetch live news on a schedule, ingest it into the "
            "Phase 9.2 pipeline, generate alerts, and (optionally) notify the user."
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
        help="Number of scheduled passes (with --interval; default: 1).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLAlchemy database URL (default: in-memory SQLite).",
    )
    parser.add_argument(
        "--no-alerts",
        action="store_true",
        help="Skip alert generation (default: generate alerts from ingested items).",
    )
    parser.add_argument(
        "--notify",
        choices=list(NOTIFY_CHOICES),
        default="log",
        help="Notification channel (default: log, which is safe and offline).",
    )
    parser.add_argument(
        "--slack-webhook-url",
        default=os.environ.get("SLACK_WEBHOOK_URL"),
        help="Slack incoming-webhook URL (or SLACK_WEBHOOK_URL env).",
    )
    parser.add_argument("--email-from", default=None, help="Sender address for email.")
    parser.add_argument("--email-to", default=None, help="Comma-separated recipients.")
    parser.add_argument(
        "--smtp-host", default=os.environ.get("SMTP_HOST") or "localhost"
    )
    parser.add_argument("--smtp-port", type=int, default=25)
    parser.add_argument("--smtp-user", default=os.environ.get("SMTP_USERNAME"))
    parser.add_argument("--smtp-password", default=os.environ.get("SMTP_PASSWORD"))
    return parser.parse_args(argv)


def _build_session(db_url: str | None) -> Any:
    """Return a sessionmaker bound to ``db_url`` (or an in-memory SQLite)."""
    if db_url is None:
        engine = create_engine("sqlite:///:memory:", echo=False)
    else:
        engine = create_engine(db_url, echo=False)

    if str(engine.url).startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
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


def build_notifiers(args: argparse.Namespace) -> list[Notifier]:
    """Build the requested notification channels. Returns [] for ``none``.

    Raises ``ValueError`` on a usage/configuration problem so ``main`` can map
    it to ``EXIT_USAGE`` without touching the network.
    """
    if args.notify == "none":
        return []
    if args.notify == "log":
        return [LogNotifier()]

    if args.notify == "slack":
        if not args.slack_webhook_url:
            raise ValueError(
                "--notify slack requires --slack-webhook-url or SLACK_WEBHOOK_URL."
            )
        return [SlackNotifier(args.slack_webhook_url)]

    # email
    recipients = [a.strip() for a in (args.email_to or "").split(",") if a.strip()]
    if not args.email_from or not recipients:
        raise ValueError(
            "--notify email requires --email-from and --email-to (comma-separated)."
        )
    return [
        EmailNotifier(
            args.email_from,
            recipients,
            host=args.smtp_host,
            port=args.smtp_port,
            username=args.smtp_user,
            password=args.smtp_password,
        )
    ]


def _print_run(run: SchedulerRunReport, *, dry_run: bool) -> None:
    report = run.connector_report
    print("=" * 78)
    print("PHASE 9.6 — SCHEDULING & ALERTING PASS SUMMARY")
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
    if run.alerts:
        print("  alerts         :")
        for alert in run.alerts:
            print(
                f"    - [{alert.alert_type.value}/{alert.severity.value}] {alert.title}"
            )
    if run.notifications is not None:
        notifications = run.notifications
        print(
            "  notifications  : "
            f"alerts={notifications.alerts} attempted={notifications.attempted} "
            f"delivered={notifications.delivered} failed={notifications.failed}"
        )
        for receipt in notifications.receipts:
            status = "ok" if receipt.ok else f"FAILED: {receipt.error}"
            print(f"    - {receipt.channel}: {status}")
    for err in run.errors:
        print(f"  pass error     : {err}")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.interval is not None and args.interval <= 0:
        print("USAGE ERROR: --interval must be positive.")
        return EXIT_USAGE
    if args.iterations < 1:
        print("USAGE ERROR: --iterations must be at least 1.")
        return EXIT_USAGE

    try:
        notifiers = build_notifiers(args)
    except ValueError as exc:
        print(f"USAGE ERROR: {exc}")
        return EXIT_USAGE

    SessionLocal = _build_session(args.db)
    db = SessionLocal()

    notification_service = (
        NotificationService(notifiers, min_interval_seconds=0.5) if notifiers else None
    )
    alert_generator = None if args.no_alerts else AlertGenerator(max_alerts_per_pass=50)

    scheduler: Scheduler | None = None
    try:
        connectors = build_connectors(args)

        def ingest(
            raw: RawItem, *, connector: SourceConnector, dry_run: bool
        ) -> Any:
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

        scheduler = Scheduler(
            connectors,
            ingest=ingest,
            alert_generator=alert_generator,
            notification_service=notification_service,
        )
        connector_flag = None if args.connector == "all" else args.connector

        if args.interval is not None:
            runs = scheduler.run_scheduled(
                interval_seconds=args.interval,
                iterations=args.iterations,
                connector=connector_flag,
                dry_run=args.dry_run,
            )
            for i, run in enumerate(runs, start=1):
                if len(runs) > 1:
                    print(f"\n--- pass {i}/{len(runs)} ---")
                _print_run(run, dry_run=args.dry_run)
        else:
            run = scheduler.run(connector=connector_flag, dry_run=args.dry_run)
            _print_run(run, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - report network/provider errors cleanly
        print(f"PROVIDER ERROR: {exc}")
        return EXIT_PROVIDER
    finally:
        if notification_service is not None:
            notification_service.close()
        if scheduler is not None:
            for conn in scheduler.connectors.values():
                conn.close()
        db.close()

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())