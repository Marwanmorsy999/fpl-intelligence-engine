"""Audit migrations 0024 and 0025 against the LIVE Supabase DB.

Reports:
- alembic_version: current head
- decision_snapshots: exists, columns, indexes, row count
- predictions_current: PK + UNIQUE constraints, computed_at index
- availability_events.primary_source_id: index existence

Read-only. No DDL. Safe to run any time.

Reads DATABASE_URL from .env.local (gitignored).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

_ENV_FILE = ".env.local"


def _read_env_local() -> dict[str, str]:
    path = Path(_ENV_FILE)
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    env = _read_env_local()
    db_url = env.get("DATABASE_URL", "")
    if not db_url:
        print("DATABASE_URL missing in .env.local")
        return 2

    from sqlalchemy import create_engine, text

    engine = create_engine(db_url, connect_args={"connect_timeout": 30})
    out: list[str] = []
    out.append(f"# Live-DB audit — {datetime.now(tz=UTC).isoformat()}\n")
    host = db_url.split("@")[-1]
    out.append(f"Host: {host}\n")

    def run_one(label: str, sql: str) -> str:
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(sql)).fetchall()
            if not rows:
                return f"{label}: NONE\n"
            if len(rows[0]) == 1:
                return f"{label}: {[r[0] for r in rows]}\n"
            return f"{label}: {[(r[0], r[1]) for r in rows]}\n"
        except Exception as exc:
            msg = str(exc).split("\n")[0][:200]
            return f"{label}: ERROR {msg}\n"

    out.append("\nalembic_version: ")
    out.append(run_one("value", "SELECT version_num FROM alembic_version"))

    out.append("\n## decision_snapshots (migration 0025)\n")
    out.append(
        run_one(
            "columns",
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='decision_snapshots' ORDER BY ordinal_position",
        )
    )
    out.append(
        run_one(
            "indexes",
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='decision_snapshots'",
        )
    )
    out.append(run_one("row_count", "SELECT COUNT(*) FROM decision_snapshots"))

    out.append("\n## predictions_current (migration 0024)\n")
    out.append(
        run_one(
            "constraints",
            "SELECT conname, contype FROM pg_constraint "
            "WHERE conrelid='public.predictions_current'::regclass "
            "ORDER BY contype, conname",
        )
    )
    out.append(
        run_one(
            "indexes",
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='predictions_current' "
            "ORDER BY indexname",
        )
    )

    out.append("\n## availability_events.primary_source_id (migration 0024)\n")
    out.append(
        run_one(
            "primary_source_id indexes",
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='availability_events' "
            "AND indexdef LIKE '%primary_source_id%' "
            "ORDER BY indexname",
        )
    )

    Path("docs").mkdir(parents=True, exist_ok=True)
    Path("docs/LIVE_DB_AUDIT_2026.md").write_text("".join(out), encoding="utf-8")
    print("Wrote docs/LIVE_DB_AUDIT_2026.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
