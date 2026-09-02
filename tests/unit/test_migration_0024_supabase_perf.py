"""Validate migration 0024 (Supabase perf evidence) without applying it.

Uses a recording alembic operations mock so we can assert exactly what the
migration would emit. This catches accidental schema changes (table names,
column names, index definitions) before any DDL hits Supabase.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_MIG_PATH = Path("migrations/versions/0024_supabase_perf_evidence.py")


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0024", _MIG_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Recorder:
    """Record ``op.execute`` and ``op.<index op>`` calls."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.creates: list[tuple[str, str, list[str] | None, bool]] = []
        self.drops: list[tuple[str, str | None]] = []

    def execute(self, sql: Any) -> None:
        self.sql.append(str(sql))

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        *,
        unique: bool = False,
        **kw: Any,
    ) -> None:
        self.creates.append((name, table_name, list(columns), bool(unique)))

    def drop_index(self, name: str, *, table_name: str | None = None, **kw: Any) -> None:
        self.drops.append((name, table_name))

    def add_constraint(self, *args: Any, **kw: Any) -> None:
        self.sql.append(f"add_constraint({args}, {kw})")

    def drop_constraint(self, *args: Any, **kw: Any) -> None:
        self.sql.append(f"drop_constraint({args}, {kw})")


def test_migration_metadata() -> None:
    mig = _load_migration()
    assert mig.revision == "0024_supabase_perf_evidence"
    assert mig.down_revision == "0023_performance_security_cleanup"
    assert mig.branch_labels is None
    assert mig.depends_on is None


def test_upgrade_emits_expected_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()

    # 1. PK promotion using the existing UNIQUE index.
    pk_sql = "\n".join(rec.sql)
    assert "predictions_current_pkey" in pk_sql
    assert "USING INDEX uq_pred_current_gw_element" in pk_sql

    # 2. predictions_current_computed_at_idx on predictions_current(computed_at)
    pc_idx = [c for c in rec.creates if c[0] == "predictions_current_computed_at_idx"]
    assert len(pc_idx) == 1
    name, table, cols, unique = pc_idx[0]
    assert table == "predictions_current"
    assert cols == ["computed_at"]
    assert unique is False

    # 3. ix_availability_events_primary_source_id
    ae_idx = [c for c in rec.creates if c[0] == "ix_availability_events_primary_source_id"]
    assert len(ae_idx) == 1
    name, table, cols, unique = ae_idx[0]
    assert table == "availability_events"
    assert cols == ["primary_source_id"]
    assert unique is False


def test_downgrade_reverses_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.downgrade()

    drop_names = {d[0] for d in rec.drops}
    assert "ix_availability_events_primary_source_id" in drop_names
    assert "predictions_current_computed_at_idx" in drop_names

    drop_pk_sql = "\n".join(rec.sql)
    assert "DROP CONSTRAINT predictions_current_pkey" in drop_pk_sql


def test_ddl_targets_are_evidence_tables_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catch accidental schema drift: only the documented tables change."""
    expected_tables = {"predictions_current", "availability_events"}
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    tables = {c[1] for c in rec.creates}
    assert tables <= expected_tables, f"unexpected tables touched: {tables - expected_tables}"
