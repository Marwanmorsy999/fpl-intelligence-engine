"""Apply Alembic migrations explicitly during deploy (v2.7.4-prod-heal).

Runs ``alembic upgrade head`` against $DATABASE_URL so a fresh deployment can
never serve against a schema that predates its own code (the 0021 gap that
took /league and /league/trajectory down). Wired into the Vercel build via
vercel.json's buildCommand::

    pip install . && python -m fpl_intelligence.prod_migrate

Exit codes: 0 migrated/healed, 2 migration failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.config import Config

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _normalized_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print(
            "prod_migrate: DATABASE_URL not set — skipping migration "
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
        print("prod_migrate: alembic upgrade head")
        command.upgrade(cfg, "head")
    except Exception as exc:  # noqa: BLE001 — deploy must stop on failure
        msg = f"{type(exc).__name__}: {exc}"
        # Prod drift pattern: the schema is AHEAD of alembic_version (tables
        # created by create_all or manual DDL). "already exists"-class
        # failures mean exactly that. Repair = (1) create every model table
        # still missing (idempotent; this is what heals e.g. 0021), then
        # (2) align the version bookkeeping with reality.
        if "already exists" in str(exc) or "DuplicateTable" in msg or "DuplicateColumn" in msg:
            print(
                "prod_migrate: schema ahead of alembic_version "
                f"({msg.splitlines()[0][:160]}) — healing drift"
            )
            try:
                _create_missing_tables(url)
                command.stamp(cfg, "head")
            except Exception as exc2:  # noqa: BLE001
                print(f"prod_migrate heal FAILED: {exc2}", file=sys.stderr)
                return 2
            print("prod_migrate: drift healed, stamped to head")
        elif "overlaps" in str(exc):
            # Bookkeeping corruption: alembic_version holds multiple rows, so
            # every upgrade/stamp request trips an overlap check before any
            # SQL runs. Keep only the head-most KNOWN revision and retry —
            # schema objects are untouched.
            if not _normalize_version_rows(url, cfg):
                print(
                    "prod_migrate FAILED: could not normalise corrupted "
                    f"alembic_version rows ({msg})",
                    file=sys.stderr,
                )
                return 2
            try:
                command.upgrade(cfg, "head")
            except Exception as exc3:  # noqa: BLE001
                print(f"prod_migrate FAILED after repair: {exc3}", file=sys.stderr)
                return 2
        else:
            print(f"prod_migrate FAILED: {msg}", file=sys.stderr)
            return 2
    print("prod_migrate: OK")
    return 0


def _normalize_version_rows(url: str, cfg: Config) -> bool:
    """Collapse duplicate ``alembic_version`` rows to the head-most one.

    Returns True when a known revision row was kept (and the rest removed).
    """
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    script_dir = ScriptDirectory.from_config(cfg)
    order = [rev.revision for rev in script_dir.walk_revisions()]  # head-first
    rank = {rev: i for i, rev in enumerate(order)}

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version ("
                    "version_num VARCHAR(32) NOT NULL)"
                )
            )
            rows = [
                r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))
            ]
            if len(rows) <= 1:
                return False
            keep = min((r for r in rows if r in rank), key=lambda r: rank[r], default=None)
            if keep is None:
                return False
            drop = [r for r in rows if r != keep]
            for stale in drop:
                conn.execute(
                    text("DELETE FROM alembic_version WHERE version_num = :v"),
                    {"v": stale},
                )
            print(
                f"prod_migrate: alembic_version had {len(rows)} rows; kept "
                f"{keep!r}, removed {drop}"
            )
            return True
    finally:
        engine.dispose()


def _create_missing_tables(url: str) -> None:
    """Idempotently create any model tables missing from the database."""
    from sqlalchemy import create_engine, inspect

    # Register EVERY ORM table on the shared metadata (mirrors
    # migrations/env.py plus the runtime-created league/transfer/price/push
    # models so a drift heal never skips a table the app expects).
    from fpl_intelligence.availability import models as _availability_models  # noqa: F401,PLC0415
    from fpl_intelligence.db.base import Base
    from fpl_intelligence.leagues import models as _league_models  # noqa: F401,PLC0415
    from fpl_intelligence.live_intelligence import models as _live_models  # noqa: F401,PLC0415
    from fpl_intelligence.notifications.webpush import NotificationLogDB as _n  # noqa: F401,PLC0415
    from fpl_intelligence.prices.models import PriceMoveDB as _pm  # noqa: F401,PLC0415
    from fpl_intelligence.prices.models import PriceSnapshotDB as _ps  # noqa: F401,PLC0415
    from fpl_intelligence.squad import models_db as _squad_models  # noqa: F401,PLC0415
    from fpl_intelligence.sync import materialized_models as _materialized  # noqa: F401,PLC0415
    from fpl_intelligence.sync import models as _sync_models  # noqa: F401,PLC0415
    from fpl_intelligence.transfers import models as _transfer_models  # noqa: F401,PLC0415

    engine = create_engine(url)
    try:
        insp = inspect(engine)
        existing = set(insp.get_table_names())
        missing = [t for t in Base.metadata.sorted_tables if t.name not in existing]
        if missing:
            names = ", ".join(t.name for t in missing)
            print(f"prod_migrate: creating missing tables: {names}")
            Base.metadata.create_all(engine, tables=missing)
        else:
            print("prod_migrate: no missing model tables")
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
