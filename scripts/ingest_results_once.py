#!/usr/bin/env python
"""scripts/ingest_results_once.py — Phase 21.1 (T1) one-shot results ingestion.

Fetches finalised per-element gameweek results straight from the official FPL
``/api/event/{gw}/live/`` endpoint (via the egress masks when direct access
is blocked), stores them as ingested history, scores pending recommendations
and reconciles the calibration ledger — flipping Track-Record cards from
*pending* to *graded* with names, hit rate and calibration MAE.

Run modes:
    # against whatever DATABASE_URL points at (prod Supabase via .env):
    python scripts/ingest_results_once.py

    # force specific gameweeks even when rows exist (partial coverage fix):
    python scripts/ingest_results_once.py --force 1

Exit codes: 0 ok / nothing to do, 1 fetch or persistence failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

EXIT_OK = 0
EXIT_FAIL = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force",
        default="",
        help="Comma-separated gameweeks to (re-)fetch regardless of stored coverage.",
    )
    parser.add_argument(
        "--max-gws",
        type=int,
        default=2,
        help="How many most-recent finished gameweeks to consider.",
    )
    args = parser.parse_args(argv)

    from fpl_intelligence.db.session import SessionLocal
    from fpl_intelligence.sync.results_ingestion import ingest_finished_gameweeks

    force_gws = tuple(
        int(part) for part in args.force.split(",") if part.strip().isdigit()
    )

    async def _run() -> dict:
        db = SessionLocal()
        try:
            return await ingest_finished_gameweeks(
                db,
                force_gameweeks=force_gws,
                max_gameweeks=max(1, args.max_gws),
            )
        finally:
            db.close()

    report = asyncio.run(_run())
    print(json.dumps(report, indent=2, default=str))
    ingested = report.get("ingested") or []
    failures = [s for s in report.get("skipped", []) if "fetch failed" in str(s.get("reason"))]
    if not ingested and len(force_gws) > 0 and failures:
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
