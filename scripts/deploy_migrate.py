#!/usr/bin/env python
"""Apply Alembic migrations explicitly during deploy (v2.7.4-prod-heal).

Runs ``alembic upgrade head`` against $DATABASE_URL so a fresh deployment can
never serve against a schema that predates its own code (the 0021 gap that
took /league and /league/trajectory down). Wired into the Vercel build via
vercel.json's buildCommand.

Exit codes: 0 migrated, 1 misconfiguration, 2 migration failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _normalized_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print(
            "deploy_migrate: DATABASE_URL not set — skipping migration "
            "(local/dev build)",
            file=sys.stderr,
        )
        raise SystemExit(0)
    # Supabase/Heroku style URLs need the psycopg driver for SQLAlchemy 2.x.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def main() -> int:
    url = _normalized_url()

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)

    try:
        print("deploy_migrate: alembic upgrade head")
        command.upgrade(cfg, "head")
    except Exception as exc:  # noqa: BLE001 — deploy must stop on failure
        print(f"deploy_migrate FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("deploy_migrate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
