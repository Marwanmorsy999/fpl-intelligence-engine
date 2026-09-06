"""Render the exact DDL migration 0024 upgrade() will emit.

Like the 0025 renderer, this works around the broken
``alembic upgrade --sql`` path (migration 0011's ``_enum_values``
call requires a live DB connection) by running the migration
against a recording op-stub and formatting the captured operations.

Output: ``docs/MIGRATION_0024_DDL.md`` — paste into the Supabase SQL
editor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _load_migration() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mig_0024", Path("migrations/versions/0024_supabase_perf_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render_ddl(mig: Any) -> str:
    """Capture the migration's calls and render them as SQL.

    Migration 0024 uses raw ``op.execute(...)`` for the PK promotion
    and ``op.create_index(...)`` for the two new indexes. We record
    both call shapes.
    """
    captured: dict[str, Any] = {
        "execute": [],
        "create_index": [],
        "drop_index": [],
        "drop_constraint": [],
    }

    class _Rec:
        def execute(self, sql: str) -> None:
            captured["execute"].append(str(sql))

        def create_index(
            self,
            name: str,
            table: str,
            columns: list[str],
            **kw: Any,
        ) -> None:
            captured["create_index"].append(
                (name, table, list(columns), bool(kw.get("unique", False)))
            )

        def drop_index(
            self,
            name: str,
            *,
            table_name: str | None = None,
            **kw: Any,
        ) -> None:
            captured["drop_index"].append((name, table_name))

        def create_table(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
            raise NotImplementedError("0024 does not create tables")

        def drop_table(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
            raise NotImplementedError("0024 does not drop tables")

    mig.op = _Rec()
    mig.upgrade()

    out: list[str] = ["-- === MIGRATION 0024_supabase_perf_evidence UPGRADE ===", "BEGIN;"]
    for sql in captured["execute"]:
        out.append(f"\n{sql};\n")
    for name, table, columns, unique in captured["create_index"]:
        cols_str = ", ".join(f'"{c}"' for c in columns)
        kw = "UNIQUE " if unique else ""
        out.append(f"\nCREATE {kw}INDEX {name} ON public.{table} ({cols_str});\n")
    out.append("COMMIT;")

    # Downgrade
    captured2: dict[str, Any] = {"execute": [], "drop_index": []}

    class _Rec2:
        def execute(self, sql: str) -> None:
            captured2["execute"].append(str(sql))

        def drop_index(
            self,
            name: str,
            *,
            table_name: str | None = None,
            **kw: Any,
        ) -> None:
            captured2["drop_index"].append((name, table_name))

    mig.op = _Rec2()
    mig.downgrade()
    out.append("\n-- === MIGRATION 0024_supabase_perf_evidence DOWNGRADE ===")
    out.append("BEGIN;")
    for sql in captured2["execute"]:
        out.append(f"{sql};")
    for name, _table in captured2["drop_index"]:
        out.append(f"DROP INDEX IF EXISTS public.{name};")
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def main() -> int:
    mig = _load_migration()
    ddl = render_ddl(mig)
    out_path = Path("docs/MIGRATION_0024_DDL.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(ddl, encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
