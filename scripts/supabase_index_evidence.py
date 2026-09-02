"""Capture EXPLAIN ANALYZE plans for the hot paths called out in issue #24.

Runs read-only EXPLAIN (ANALYZE, BUFFERS) against the live Supabase Postgres
configured via ``.env.local`` (gitignored) and writes a Markdown evidence
report to ``docs/SUPABASE_INDEX_EVIDENCE_2026.md``.

This script only **reads** schema statistics and query plans. It does not
modify the database.

Usage:
    python scripts/supabase_index_evidence.py
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("_ENV_FILE", ".env.local")

from sqlalchemy import create_engine, text  # noqa: I001,E402

from fpl_intelligence.config.settings import Settings  # noqa: E402


HOT_QUERIES: list[tuple[str, str, str]] = [
    (
        "Q1: predictions_current lookup by gameweek (planner/targets/league routes)",
        "select",
        textwrap.dedent(
            """
            SELECT element_id, expected_points
            FROM predictions_current
            WHERE gameweek = 1
            """
        ).strip(),
    ),
    (
        "Q2: predictions_current last-computed (data sources endpoint)",
        "select",
        textwrap.dedent(
            """
            SELECT computed_at
            FROM predictions_current
            ORDER BY computed_at DESC
            LIMIT 1
            """
        ).strip(),
    ),
    (
        "Q3: predictions_current existence + counts per gameweek",
        "select",
        textwrap.dedent(
            """
            SELECT gameweek, COUNT(*)
            FROM predictions_current
            GROUP BY gameweek
            ORDER BY gameweek
            """
        ).strip(),
    ),
    (
        "Q4: availability_events joined to source by primary_source_id (DBAvailabilityProvider)",
        "select",
        textwrap.dedent(
            """
            SELECT s.name, s.reliability, s.is_official_club
            FROM availability_events e
            JOIN availability_sources s ON s.id = e.primary_source_id
            WHERE e.player_id = 1
            """
        ).strip(),
    ),
    (
        "Q5: availability_events range by valid_from (cutoff lookups)",
        "select",
        textwrap.dedent(
            """
            SELECT id, player_id, primary_source_id, valid_from, valid_to
            FROM availability_events
            WHERE valid_from <= NOW()
              AND (valid_to IS NULL OR valid_to > NOW())
            """
        ).strip(),
    ),
    (
        "Q6: player_team_memberships latest membership for one player (gameweek_resolve)",
        "select",
        textwrap.dedent(
            """
            SELECT team_id
            FROM player_team_memberships
            WHERE player_id = 1
            ORDER BY valid_from DESC NULLS LAST
            LIMIT 1
            """
        ).strip(),
    ),
    (
        "Q7: fixtures lookup by gameweek_id + team_id (safe_fixture_count)",
        "select",
        textwrap.dedent(
            """
            SELECT id
            FROM fixtures
            WHERE gameweek_id = 1
              AND postponed = false
              AND (home_team_id = 1 OR away_team_id = 1)
            """
        ).strip(),
    ),
    (
        "Q8: gameweek resolve by provider_event_id (gameweek_resolve.resolve_gameweek_id)",
        "select",
        textwrap.dedent(
            """
            SELECT gw.id
            FROM gameweeks gw
            JOIN seasons s ON s.id = gw.season_id
            WHERE gw.provider_event_id = 1
            ORDER BY s.code DESC
            LIMIT 1
            """
        ).strip(),
    ),
    (
        "Q9: transfer_log recent by entry + gameweek (transfer decisions path)",
        "select",
        textwrap.dedent(
            """
            SELECT element_in, element_out, gameweek, created_at
            FROM transfer_log
            WHERE entry_id = '1'
            ORDER BY gameweek DESC, created_at DESC
            LIMIT 20
            """
        ).strip(),
    ),
]


def _section(title: str) -> str:
    return f"\n## {title}\n"


def _table_row(name: str, exists: bool, n_live_rows: int | None) -> str:
    exists_s = "yes" if exists else "NO"
    rows_s = str(n_live_rows) if n_live_rows is not None else "n/a"
    return f"| `{name}` | {exists_s} | {rows_s} |\n"


def _row_counts(engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in (
        "predictions_current",
        "availability_events",
        "availability_sources",
        "player_team_memberships",
        "fixtures",
        "gameweeks",
        "seasons",
        "transfer_log",
        "players",
        "teams",
    ):
        try:
            with engine.connect() as conn:
                n = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
                counts[table] = int(n)
        except Exception as exc:  # noqa: BLE001 — informational only
            counts[table] = -1
            print(f"  count({table}) failed: {exc}")
    return counts


def _index_list(engine, table: str) -> list[dict[str, object]]:
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = 'public' AND tablename = :t
                    ORDER BY indexname
                    """
                ),
                {"t": table},
            ).fetchall()
        return [{"name": r[0], "definition": r[1]} for r in rows]
    except Exception:
        return []


def _safe_explain(engine, sql: str) -> tuple[str | None, str | None]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '15s'"))
            plan_rows = conn.execute(
                text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")
            ).scalar()
            return json.dumps(plan_rows, indent=2), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc).split("\n")[0][:200]


def main() -> int:
    settings = Settings(_env_file=".env.local")
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )
    print(f"Connecting to {settings.database_url.split('@')[-1]} ...", flush=True)

    out: list[str] = []
    out.append("# Supabase index evidence — 2026\n")
    out.append(f"Captured: {datetime.now(tz=UTC).isoformat()}\n")
    out.append("\nHost: ")
    out.append(settings.database_url.split("@")[-1])
    out.append("\n")

    with engine.connect() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '15s'"))
        # Ping
        ok = conn.execute(text("SELECT 1")).scalar()
        if ok != 1:
            out.append("Could not connect.\n")
            return 1

        out.append("\n## Row counts (live)\n\n")
        out.append("| table | exists | rows |\n|---|:---:|---:|\n")
        counts = _row_counts(engine)
        for t, n in counts.items():
            out.append(_table_row(t, n >= 0, n if n >= 0 else None))

        out.append(_section("Existing indexes (live)"))
        for table in (
            "predictions_current",
            "availability_events",
            "player_team_memberships",
            "fixtures",
            "gameweeks",
            "transfer_log",
        ):
            out.append(f"\n### `{table}`\n\n")
            indexes = _index_list(engine, table)
            if not indexes:
                out.append("_(none)_\n")
                continue
            out.append("| index | definition |\n|---|---|\n")
            for idx in indexes:
                defn = str(idx["definition"]).replace("|", "\\|")
                out.append(f"| `{idx['name']}` | {defn} |\n")

        out.append(_section("EXPLAIN ANALYZE for hot queries"))

        for title, _kind, sql in HOT_QUERIES:
            out.append(f"\n### {title}\n\n```sql\n{sql}\n```\n\n")
            plan_text, err = _safe_explain(engine, sql)
            if plan_text is None:
                out.append(f"_(failed: {err})_\n")
            else:
                out.append("```json\n")
                out.append(plan_text)
                out.append("\n```\n")

        # Specifically: is predictions_current missing a PK?
        out.append(_section("PredictionsCurrent identity check"))
        try:
            with engine.connect() as conn:
                pk_row = conn.execute(
                    text(
                        """
                        SELECT c.conname, c.contype
                        FROM pg_constraint c
                        JOIN pg_class t ON t.oid = c.conrelid
                        WHERE t.relname = 'predictions_current'
                          AND c.contype IN ('p', 'u')
                        """
                    )
                ).fetchall()
                out.append("\nPK / UNIQUE constraints on `predictions_current`:\n\n")
                if pk_row:
                    for r in pk_row:
                        out.append(f"- `{r[0]}` ({r[1]})\n")
                else:
                    out.append("- **NONE — table has no primary key or unique constraint.**\n")

                # PredictionsCurrent sample duplicate check
                dup = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM (
                          SELECT element_id, gameweek, COUNT(*) c
                          FROM predictions_current
                          GROUP BY 1,2 HAVING COUNT(*) > 1
                        ) d
                        """
                    )
                ).scalar()
                dup_msg = (
                    f"\nDuplicate (element_id, gameweek) groups in `predictions_current`: {dup}\n"
                )
                out.append(dup_msg)
        except Exception as exc:  # noqa: BLE001
            out.append(f"_(identity check failed: {exc})_\n")

    out_path = Path("docs/SUPABASE_INDEX_EVIDENCE_2026.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(out), encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
