"""Phase 4.1 — GitHub Actions transfer auto-detection sync.

Runs the *same* transfer-detection logic used by the API
(``detect_transfers_poll`` in ``fpl_intelligence.api.routes.admin``) but driven
from a GitHub Actions runner against the production Postgres/Neon database.

Why this exists
---------------
Vercel Cron is free, but the whole point of the free-tier re-architecture is to
keep scheduled/periodic jobs OFF Vercel and onto GitHub Actions (free 2 000 min
of runner time / month) so the Vercel deployment stays stateless and the cron
budget is never exhausted. The transfer-detection cron is therefore retired
from ``vercel.json`` and owned by ``.github/workflows/gha_sync.yml``.

Exit codes
----------
0  run completed (per-session failures are reported on stdout, not fatal)
1  configuration error — ``DATABASE_URL`` missing or points at SQLite
2  the transfer-detection run itself raised unexpectedly
"""

from __future__ import annotations

import asyncio
import sys

from fpl_intelligence.api.routes.admin import detect_transfers_poll
from fpl_intelligence.config import get_settings
from fpl_intelligence.db.session import SessionLocal


def main() -> int:
    settings = get_settings()
    db_url: str = settings.database_url or ""
    if not db_url or db_url.startswith("sqlite"):
        print(
            "gha_sync: DATABASE_URL not configured (or points at local SQLite). "
            "Nothing to sync in CI — skipping. Set the DATABASE_URL secret.",
            file=sys.stderr,
        )
        return 1

    db = SessionLocal()
    try:
        results = asyncio.run(detect_transfers_poll(db, settings, max_seconds=25.0))
    except Exception as exc:  # noqa: BLE001 - surface any unexpected failure
        print(f"gha_sync: transfer detection failed: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()

    checked = len(results)
    updated = sum(1 for r in results if r.get("ok"))
    failed = checked - updated
    print(
        f"gha_sync: checked={checked} updated={updated} failed={failed}",
        flush=True,
    )
    for r in results:
        if not r.get("ok"):
            print(f"  ! session {r.get('session_id')}: {r.get('error')}", file=sys.stderr)
    # Per-session failures don't fail the job: a single bad session must never
    # block the rest. Total job failure (exit 2) is reserved for true run-level
    # errors so GitHub Actions only red-flags real problems.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
