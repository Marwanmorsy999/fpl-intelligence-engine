#!/usr/bin/env python
"""scripts/verify_live_fpl_api.py — Phase 9.7 Live FPL API Verification CLI.

Fetches live data from the **official FPL API** (``bootstrap-static``), verifies
that the API is accessible and correctly parsed into :class:`RawItem` objects,
and verifies that the data is correctly ingested into the Phase 9.2 pipeline
(``ingest_raw_text``).

Usage::

    python scripts/verify_live_fpl_api.py
    python scripts/verify_live_fpl_api.py --api-url https://fantasy.premierleague.com/api/bootstrap-static/
    python scripts/verify_live_fpl_api.py --dry-run
    python scripts/verify_live_fpl_api.py --provider real --db ./fpl.db

``--dry-run`` still fetches from the live API and runs the whole pipeline, but
rolls the ingestion transaction back so nothing is persisted. ``--provider
real`` uses a configured real LLM for evidence extraction (credentials come
from the git-ignored ``.env`` only); the default is the offline
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

from fpl_intelligence.live_intelligence.connectors import FPL_BOOTSTRAP_URL  # noqa: E402
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider  # noqa: E402
from fpl_intelligence.live_intelligence.verification import (  # noqa: E402
    FPLAPIVerifier,
    LiveSourceVerification,
    build_verification_session,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PROVIDER = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 9.7 — fetch live FPL API data, verify it parses, and verify "
            "the data is ingested into the Phase 9.2 pipeline."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--api-url",
        default=FPL_BOOTSTRAP_URL,
        help=f"Official FPL endpoint (default: {FPL_BOOTSTRAP_URL}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of player news items surfaced (default: 20).",
    )
    parser.add_argument(
        "--provider",
        choices=["mock", "real"],
        default="mock",
        help="LLM provider for evidence extraction (default: mock, fully offline).",
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


def _print_report(report: LiveSourceVerification, *, dry_run: bool) -> None:
    print("=" * 78)
    print("PHASE 9.7 — LIVE FPL API VERIFICATION")
    print("=" * 78)
    print(f"  source         : {report.source}")
    print(f"  fetched        : {report.fetched}")
    print(f"  parsed         : {report.parsed}")
    print(f"  ingested       : {report.ingested}")
    print(f"  duplicates     : {report.duplicates}")
    if report.sample_titles:
        print("  sample titles  :")
        for title in report.sample_titles:
            print(f"    - {title}")
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

    session_factory = (
        build_verification_session(f"sqlite:///{args.db}") if args.db else None
    )
    verifier = FPLAPIVerifier(
        api_url=args.api_url,
        session_factory=session_factory,
        llm_provider=_build_provider(args),
    )
    try:
        report = verifier.verify(limit=args.limit, persist=not args.dry_run)
    except Exception as exc:  # noqa: BLE001 - report verification errors cleanly
        print(f"PROVIDER ERROR: {exc}")
        return EXIT_PROVIDER
    finally:
        verifier.connector.close()

    _print_report(report, dry_run=args.dry_run)
    return EXIT_OK if report.passed else EXIT_PROVIDER


if __name__ == "__main__":
    raise SystemExit(main())
