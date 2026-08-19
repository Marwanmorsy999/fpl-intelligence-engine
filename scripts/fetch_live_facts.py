#!/usr/bin/env python
"""scripts/fetch_live_facts.py — Phase 11.1 Live Fact fetcher CLI.

Fetches hard, decision-critical facts from structured public APIs and prints a
summary of what was discovered, without ever touching the LLM extraction layer:

    python scripts/fetch_live_facts.py
    python scripts/fetch_live_facts.py --cache-dir data/cache/live_facts
    python scripts/fetch_live_facts.py --date 2026-08-22
    python scripts/fetch_live_facts.py --dry-run

* The official FPL API is always used (no key required).
* API-Football and football-data.org are used only when their keys
  (``API_FOOTBALL_KEY``, ``FOOTBALL_DATA_ORG_KEY``) are present in the
  environment; otherwise they disable themselves and the run degrades
  gracefully to the facts FPL provides.

``--dry-run`` performs the same collection but never persists anything and never
makes a live LLM call. ``--cache-dir`` reuses cached API responses between runs
so external APIs are not hammered.

Exit codes: ``0`` success, ``1`` usage error, ``2`` collection failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(_SRC))

from fpl_intelligence.data_providers import (  # noqa: E402
    FactCollectionService,
    LiveFactResult,
    ResponseCache,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_COLLECT = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 11.1 — fetch live structured FPL facts and summarise them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date",
        default=None,
        help="ISO date (YYYY-MM-DD) used to scope API-Football lineups/injuries.",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Directory to store/reuse cached API responses.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect facts but persist nothing and make no LLM calls.",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="Football season year used by API-Football injury queries.",
    )
    return parser.parse_args(argv)


def summarize(result: LiveFactResult, fpl_fact_count: int) -> str:
    """Render a human-readable summary of the discovered hard facts."""
    lines: list[str] = []
    lines.append("=== Phase 11.1 Live Fact Summary ===")
    diag = result.diagnostics
    fpl = diag.get("fpl_official", {})
    lines.append(
        f"Official FPL: enabled={fpl.get('enabled', False)} "
        f"player_facts={fpl.get('facts', fpl_fact_count)}"
    )
    for key in ("api_football", "football_data_org"):
        src = diag.get(key, {})
        enabled = src.get("enabled", False)
        if not enabled:
            lines.append(f"{key}: DISABLED ({src.get('reason', 'no key')})")
        else:
            lines.append(
                f"{key}: enabled={enabled} facts={src.get('facts', 0)}"
            )
    lines.append("")
    lines.append(f"Hard fact overrides discovered: {len(result.overrides)}")
    for override in result.overrides[:20]:
        bits = []
        if override.start_probability is not None:
            bits.append(f"start={override.start_probability:.2f}")
        if override.expected_minutes is not None:
            bits.append(f"mins={override.expected_minutes:.0f}")
        if override.availability_status:
            bits.append(f"status={override.availability_status}")
        detail = ", ".join(bits) if bits else "context only"
        lines.append(f"  player {override.player_id} [{override.source.value}]: {detail}")
    if len(result.overrides) > 20:
        lines.append(f"  ... and {len(result.overrides) - 20} more")
    return "\n".join(lines)


def run(
    args: argparse.Namespace,
    *,
    service: FactCollectionService | None = None,
) -> int:
    cache = ResponseCache(cache_dir=args.cache_dir) if args.cache_dir else ResponseCache()
    if service is None:
        service = FactCollectionService(cache=cache)

    try:
        result = service.collect_overrides(date=args.date, season=args.season)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the CLI
        print(f"Live fact collection failed: {exc}", file=sys.stderr)
        return EXIT_COLLECT

    try:
        fpl = service._build_fpl() if service._fpl is None else service._fpl
        fpl_facts = fpl.collect_player_facts() if fpl is not None else []
    except Exception:  # noqa: BLE001 - best-effort extra count
        fpl_facts = []

    print(summarize(result, len(fpl_facts)))
    if args.dry_run:
        print("(dry-run: nothing persisted)")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
