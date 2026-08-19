#!/usr/bin/env python
"""scripts/verify_live_end_to_end.py — Phase 9.7 Live End-to-End Verification CLI.

Runs the **full live ingestion pipeline** against real RSS feeds and the
official FPL API and verifies every stage end-to-end:

1. fetch — the connectors reach the sources and parse the payloads;
2. ingest — the items flow into the Phase 9.2 ``ingest_raw_text`` pipeline;
3. extract — an LLM extraction run is produced for every item;
4. resolve — entities are resolved and unresolved/ambiguous evidence handled;
5. synthesize — evidence is synthesised with quantitative predictions into an
   ``IntelligenceReport`` (Phase 9.4 bridge + AI Analyst);
6. alert / notify — alerts are generated and delivered to the user (Phase 9.6).

Usage::

    python scripts/verify_live_end_to_end.py
    python scripts/verify_live_end_to_end.py --connector all --limit 5
    python scripts/verify_live_end_to_end.py --connector rss --rss-url https://...
    python scripts/verify_live_end_to_end.py --connector fpl_api
    python scripts/verify_live_end_to_end.py --dry-run
    python scripts/verify_live_end_to_end.py --provider real --db ./fpl.db

``--dry-run`` still fetches from the live sources and runs every stage, but
rolls the ingestion back (report synthesis is skipped because the evidence was
not persisted). ``--provider real`` uses a configured real LLM (credentials from
the git-ignored ``.env`` only); the default is the offline
:class:`MockLLMProvider`.

Exit codes: ``0`` all checks passed, ``1`` usage/configuration error,
``2`` verification/provider/network failure.
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

from fpl_intelligence.live_intelligence.analyst import AnalystTask  # noqa: E402
from fpl_intelligence.live_intelligence.connectors import (  # noqa: E402
    FPLAPIConnector,
    RSSConnector,
    SourceConnector,
)
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider  # noqa: E402
from fpl_intelligence.live_intelligence.verification import (  # noqa: E402
    DEFAULT_FPL_BOOTSTRAP_URL,
    DEFAULT_RSS_FEED_URL,
    EndToEndVerification,
    EndToEndVerifier,
    build_verification_session,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PROVIDER = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 9.7 — run the live ingestion pipeline end-to-end with real "
            "RSS feeds and FPL API data, and verify every stage."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--connector",
        choices=["rss", "fpl_api", "all"],
        default="all",
        help="Which live connector(s) to run (default: all).",
    )
    parser.add_argument(
        "--rss-url",
        default=DEFAULT_RSS_FEED_URL,
        help=f"RSS feed URL (default: {DEFAULT_RSS_FEED_URL}).",
    )
    parser.add_argument(
        "--source-id",
        default="rss_feed",
        help="Phase 9.2 source identifier for the RSS connector (default: rss_feed).",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_FPL_BOOTSTRAP_URL,
        help=f"Official FPL endpoint (default: {DEFAULT_FPL_BOOTSTRAP_URL}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of items fetched per connector (default: 10).",
    )
    parser.add_argument(
        "--provider",
        choices=["mock", "real"],
        default="mock",
        help="LLM provider for extraction and synthesis (default: mock, fully offline).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and run the pipeline but roll the ingestion back (persist nothing).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite DB path. Defaults to a shared in-memory database.",
    )
    parser.add_argument(
        "--gameweek",
        type=int,
        default=1,
        help="FPL gameweek used by the report-synthesis stage (default: 1).",
    )
    parser.add_argument(
        "--season-code",
        default=None,
        help="Season code (e.g. 2025-26). When supplied with --gameweek, ingested "
        "items are classified against that gameweek's deadline (PRE_DEADLINE "
        "evidence is synthesizable into the report).",
    )
    parser.add_argument(
        "--player-id",
        type=int,
        default=None,
        help="Subject player id for the report; auto-discovered from evidence by default.",
    )
    parser.add_argument(
        "--task",
        choices=[task.value for task in AnalystTask],
        default=AnalystTask.TRANSFER_RECOMMENDATION.value,
        help="Analyst task for report synthesis (default: transfer_recommendation).",
    )
    return parser.parse_args(argv)


def _build_provider(args: argparse.Namespace) -> Any:
    """Return the LLM provider selected on the command line."""
    if args.provider == "real":
        from fpl_intelligence.live_intelligence.llm_providers import ProviderFactory
        from fpl_intelligence.live_intelligence.llm_settings import (
            LLMSettingsError,
            load_llm_settings,
        )

        try:
            settings = load_llm_settings()
            return ProviderFactory(settings).create(None, http_client=None)
        except LLMSettingsError as exc:
            print(f"CONFIGURATION ERROR: {exc}")
            raise SystemExit(EXIT_USAGE) from exc
    return MockLLMProvider()


def build_connectors(args: argparse.Namespace) -> dict[str, SourceConnector]:
    """Instantiate the live connectors selected by ``--connector``."""
    connectors: dict[str, SourceConnector] = {}
    if args.connector in ("all", "rss"):
        rss = RSSConnector(args.rss_url, source_id=args.source_id)
        connectors[rss.name] = rss
    if args.connector in ("all", "fpl_api"):
        fpl = FPLAPIConnector(api_url=args.api_url)
        connectors[fpl.name] = fpl
    return connectors


def _print_report(report: EndToEndVerification, *, dry_run: bool) -> None:
    print("=" * 78)
    print("PHASE 9.7 — LIVE END-TO-END VERIFICATION")
    print("=" * 78)
    fetched = ", ".join(
        f"{name}={count}" for name, count in report.connector_fetched.items()
    )
    ingested = ", ".join(
        f"{name}={count}" for name, count in report.connector_ingested.items()
    )
    print(f"  fetched        : {report.total_fetched} ({fetched})")
    print(f"  ingested       : {report.total_ingested} ({ingested})")
    print(f"  extraction runs: {report.extraction_runs}")
    print(
        f"  evidence       : {report.availability_evidence} availability + "
        f"{report.tactical_evidence} tactical"
    )
    print(
        f"  resolution     : resolved={report.resolved_entities} "
        f"unresolved={report.unresolved_evidence} ambiguous={report.ambiguous_entities}"
    )
    print(f"  player_id      : {report.player_id}")
    print(
        f"  reports        : {report.reports_generated} "
        f"({report.report_citations} evidence citation(s))"
    )
    print(f"  alerts         : {report.alerts}")
    print(f"  notifications  : {report.notifications_delivered} delivered")
    for step in report.steps:
        marker = "PASS" if step.ok else "FAIL"
        print(f"  [{marker}] {step.name}: {step.detail}")
    for error in report.errors:
        print(f"  error          : {error}")
    suffix = " (dry-run: fetched/parsed but nothing persisted)" if dry_run else ""
    print(f"  overall        : {'PASS' if report.passed else 'FAIL'}{suffix}")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit < 1:
        print("USAGE ERROR: --limit must be at least 1.")
        return EXIT_USAGE
    if args.gameweek < 1:
        print("USAGE ERROR: --gameweek must be at least 1.")
        return EXIT_USAGE

    session_factory = (
        build_verification_session(f"sqlite:///{args.db}") if args.db else None
    )
    verifier = EndToEndVerifier(
        connectors=build_connectors(args),
        session_factory=session_factory,
        llm_provider=_build_provider(args),
        task=args.task,
        player_id=args.player_id,
        gameweek=args.gameweek,
        season_code=args.season_code,
        gameweek_number=args.gameweek,
    )
    try:
        report = verifier.verify(limit=args.limit, persist=not args.dry_run)
    except Exception as exc:  # noqa: BLE001 - report verification errors cleanly
        print(f"PROVIDER ERROR: {exc}")
        return EXIT_PROVIDER
    finally:
        for connector in verifier.connectors.values():
            connector.close()

    _print_report(report, dry_run=args.dry_run)
    return EXIT_OK if report.passed else EXIT_PROVIDER


if __name__ == "__main__":
    raise SystemExit(main())

