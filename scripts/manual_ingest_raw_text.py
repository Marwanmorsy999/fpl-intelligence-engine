#!/usr/bin/env python
"""Phase 9.2 — Manual ingestion scaffold for the multi-source ledger.

A *controlled* entry point for feeding real-world unstructured text (press
conference transcripts, news articles, social posts) into the Phase 9.1 LLM
extraction engine while tracking provenance and preventing duplicate evidence.

This is a **manual tool, not a test**. It lives in ``scripts/`` and is never
collected by pytest. By default it uses the deterministic mock provider, so it
makes no network calls and spends no quota. Pass ``--provider real`` (with a
configured API key in ``.env``) to extract with a live model.

Pipeline
--------

    text + published_at
        -> SHA-256 content_hash
        -> duplicate check (same source + hash => skip)
        -> RawItem persisted to live_intelligence_raw_items
        -> projected into a LedgerItemView (inherits temporal fields)
        -> PromptedLLMExtractor (Phase 9.1)
        -> persist_extraction -> Phase 7/8 evidence tables
        -> printed summary + evidence ids

Usage
-----

    python scripts/manual_ingest_raw_text.py \\
        --source-id press_conference_manual \\
        --file transcript.txt \\
        --published-at 2025-08-15T14:00:00+01:00 \\
        --url https://example.com/transcript

    python scripts/manual_ingest_raw_text.py \\
        --source-id journalist_manual \\
        --text "Salah is ruled out for the next three weeks." \\
        --published-at 2025-08-15T14:00:00Z

    python scripts/manual_ingest_raw_text.py \\
        --source-id press_conference_manual --file transcript.txt \\
        --published-at 2025-08-15T14:00:00Z \\
        --season-code 2025-26 --gameweek-number 3

Exit codes: ``0`` success (including a clean duplicate skip), ``1`` usage /
configuration error, ``2`` provider error.
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(_SRC))

from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider  # noqa: E402
from fpl_intelligence.live_intelligence.raw_item_ledger import (  # noqa: E402
    ManualIngestStatus,
    ingest_raw_text,
)
from fpl_intelligence.live_intelligence.source_registry import SourceType  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PROVIDER = 2


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, assuming UTC for naive inputs."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 9.2 manual raw-text ingestion into the multi-source ledger.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source-id",
        required=True,
        help="Phase 9.2 source id, e.g. 'press_conference_manual'.",
    )
    text_group = parser.add_mutually_exclusive_group(required=True)
    text_group.add_argument("--file", type=Path, help="Path to a .txt file of raw text.")
    text_group.add_argument("--text", help="Raw text supplied directly on the command line.")
    parser.add_argument(
        "--published-at",
        required=True,
        help="ISO-8601 publication time, e.g. '2025-08-15T14:00:00+01:00'.",
    )
    parser.add_argument("--url", default=None, help="Optional source URL.")
    parser.add_argument("--external-id", default=None, help="Optional provider-side content id.")
    parser.add_argument("--title", default=None, help="Optional display title.")
    parser.add_argument(
        "--source-type",
        choices=[t.value for t in SourceType],
        default=None,
        help="Override the source type (else inferred from presets).",
    )
    parser.add_argument(
        "--available-at",
        default=None,
        help="ISO-8601 availability time (defaults to --published-at).",
    )
    parser.add_argument(
        "--temporal-class",
        default=None,
        choices=["pre_deadline", "post_match", "post_deadline", "no_deadline_context"],
        help="Explicit temporal class when no gameweek context is supplied.",
    )
    parser.add_argument("--season-code", default=None, help="e.g. '2025-26'.")
    parser.add_argument("--gameweek-number", type=int, default=None, help="e.g. 3.")
    parser.add_argument(
        "--provider",
        choices=["mock", "real"],
        default="mock",
        help="Extraction provider. 'mock' (default) makes no network calls.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run extraction inside a transaction and roll back at the end. "
        "No rows are permanently written, but counts and IDs are printed.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite DB path. Defaults to an in-memory database.",
    )
    return parser.parse_args(argv)


def _build_provider(args: argparse.Namespace):
    if args.provider == "real":
        from fpl_intelligence.live_intelligence.llm_providers import ProviderFactory
        from fpl_intelligence.live_intelligence.llm_settings import (
            LLMSettingsError,
            load_llm_settings,
        )

        try:
            settings = load_llm_settings()
            provider = ProviderFactory(settings).create(None, http_client=None)
        except LLMSettingsError as exc:
            print(f"CONFIGURATION ERROR: {exc}")
            raise SystemExit(EXIT_USAGE) from exc
        return provider
    return MockLLMProvider()


def _build_session(db_path: Path | None):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from fpl_intelligence.db.base import Base

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
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return SessionLocal


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        published_at = _parse_iso(args.published_at)
        available_at = _parse_iso(args.available_at) if args.available_at else None
    except ValueError as exc:
        print(f"USAGE ERROR: could not parse timestamp: {exc}")
        return EXIT_USAGE

    if args.file is not None:
        if not args.file.is_file():
            print(f"USAGE ERROR: file not found: {args.file}")
            return EXIT_USAGE
        text = args.file.read_text(encoding="utf-8")
    else:
        text = args.text or ""

    if not text.strip():
        print("USAGE ERROR: no non-empty text to ingest.")
        return EXIT_USAGE

    source_type = SourceType(args.source_type) if args.source_type else None

    SessionLocal = _build_session(args.db)
    db = SessionLocal()
    try:
        provider = _build_provider(args)
        try:
            report = ingest_raw_text(
                db,
                source_id=args.source_id,
                text=text,
                published_at=published_at,
                url=args.url,
                external_id=args.external_id,
                title=args.title,
                source_type=source_type,
                available_at=available_at,
                temporal_class=args.temporal_class,
                season_code=args.season_code,
                gameweek_number=args.gameweek_number,
                provider=provider,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - report provider/usage errors cleanly
            print(f"PROVIDER ERROR: {exc}")
            return EXIT_PROVIDER

        if report.duplicate:
            print("Duplicate content detected, skipping extraction")
            return EXIT_OK

        if report.status is ManualIngestStatus.REJECTED:
            print(f"REJECTED: {report.error}")
            return EXIT_USAGE

        print("=" * 78)
        print("PHASE 9.2 — MANUAL INGESTION SUMMARY")
        print("=" * 78)
        print(f"  source_id           : {report.source_id}")
        print(f"  content_hash        : {report.content_hash}")
        print(f"  raw_item_id         : {report.raw_item_id}")
        print(f"  extraction_run_id   : {report.extraction_run_id}")
        print(f"  availability_evidence: {report.availability_count}")
        print(f"  tactical_evidence   : {report.tactical_count}")
        print(f"  resolved            : {report.resolved_count}")
        print(f"  unresolved          : {report.unresolved_count}")
        print(f"  ambiguous           : {report.ambiguous_count}")
        if report.availability_evidence_ids:
            print(f"  availability_ids    : {report.availability_evidence_ids}")
        if report.tactical_evidence_ids:
            print(f"  tactical_ids        : {report.tactical_evidence_ids}")
        if report.unresolved_evidence_ids:
            print(f"  unresolved_ids      : {report.unresolved_evidence_ids}")
        if args.dry_run:
            print(f"  dry_run             : True (all changes rolled back)")
        print("=" * 78)
        return EXIT_OK
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
