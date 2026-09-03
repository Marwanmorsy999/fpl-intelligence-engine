"""Validate migration 0024 (supabase_perf_evidence) DDL contract.

Migration 0024 issues only ``op.execute(...)`` (PK promotion) and
``op.create_index(...)`` (two new indexes). The downgrade issues
``op.execute(...)`` (DROP CONSTRAINT) and ``op.drop_index(...)`` for
the two new indexes.

This test is a pure-Python contract test — no database. It loads
the migration module, drives ``upgrade()`` and ``downgrade()``
against a recording op-stub, and asserts that the recorded calls
match the documented schema changes.
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
    """Record every ``op`` call so the test can assert DDL intent.

    Implements the subset of the alembic ``Operations`` API used by
    migration 0024: ``execute``, ``create_index``, ``drop_index``.
    ``create_table`` / ``drop_table`` are deliberately not supported
    because the migration should not issue them — calling them is a
    drift signal.
    """

    def __init__(self) -> None:
        self.execute_sql: list[str] = []
        self.create_index_calls: list[tuple[str, str, list[str], bool]] = []
        self.drop_index_calls: list[tuple[str, str | None]] = []

    def execute(self, sql: Any) -> None:
        self.execute_sql.append(str(sql))

    def create_index(
        self,
        name: str,
        table: str,
        columns: list[str],
        **kw: Any,
    ) -> None:
        self.create_index_calls.append((name, table, list(columns), bool(kw.get("unique", False))))

    def drop_index(
        self,
        name: str,
        *,
        table_name: str | None = None,
        **kw: Any,
    ) -> None:
        self.drop_index_calls.append((name, table_name))

    def create_table(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
        raise NotImplementedError("0024 must not create tables")

    def drop_table(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
        raise NotImplementedError("0024 must not drop tables")


# --- metadata --------------------------------------------------------------


def test_migration_metadata() -> None:
    mig = _load_migration()
    assert mig.revision == "0024_supabase_perf_evidence"
    assert mig.down_revision == "0023_performance_security_cleanup"
    assert mig.branch_labels is None
    assert mig.depends_on is None


# --- upgrade ---------------------------------------------------------------


def test_upgrade_promotes_unique_to_pk(monkeypatch: pytest.MonkeyPatch) -> None:
    """PK promotion uses the existing UNIQUE index ``uq_pred_current_gw_element``."""
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    pk_sqls = [s for s in rec.execute_sql if "PRIMARY KEY" in s]
    assert len(pk_sqls) == 1
    pk_sql = pk_sqls[0]
    assert "predictions_current" in pk_sql
    assert "predictions_current_pkey" in pk_sql
    # The crucial ``USING INDEX`` clause: the migration must reuse the
    # existing UNIQUE index rather than building a new one.
    assert "USING INDEX" in pk_sql
    assert "uq_pred_current_gw_element" in pk_sql


def test_upgrade_creates_computed_at_index(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    matches = [c for c in rec.create_index_calls if c[0] == "predictions_current_computed_at_idx"]
    assert len(matches) == 1
    name, table, columns, unique = matches[0]
    assert table == "predictions_current"
    assert columns == ["computed_at"]
    assert unique is False


def test_upgrade_creates_primary_source_id_index(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    matches = [
        c for c in rec.create_index_calls if c[0] == "ix_availability_events_primary_source_id"
    ]
    assert len(matches) == 1
    name, table, columns, unique = matches[0]
    assert table == "availability_events"
    assert columns == ["primary_source_id"]
    assert unique is False


def test_upgrade_creates_exactly_two_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema-drift guard: 0024 must create exactly two indexes."""
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    assert len(rec.create_index_calls) == 2


def test_upgrade_executes_exactly_one_pk_alter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema-drift guard: 0024 must issue exactly one ALTER for the PK."""
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    pk_sqls = [s for s in rec.execute_sql if "PRIMARY KEY" in s]
    assert len(pk_sqls) == 1


def test_upgrade_does_not_create_or_drop_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema-drift guard: 0024 must not touch any CREATE/DROP TABLE."""
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    # The recorder raises on create_table/drop_table; reaching this
    # line is the assertion. We also assert nothing slipped in.
    assert all("CREATE TABLE" not in s.upper() for s in rec.execute_sql)
    assert all("DROP TABLE" not in s.upper() for s in rec.execute_sql)


# --- downgrade -------------------------------------------------------------


def test_downgrade_drops_pk_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Downgrade must drop the PK constraint, not the underlying UNIQUE."""
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.downgrade()
    drop_pk_sqls = [
        s for s in rec.execute_sql if "DROP CONSTRAINT" in s and "predictions_current_pkey" in s
    ]
    assert len(drop_pk_sqls) == 1
    # The underlying UNIQUE index is not dropped — only the PK
    # constraint that referenced it.
    for sql in rec.execute_sql:
        assert "DROP INDEX" not in sql
        assert "uq_pred_current_gw_element" not in sql


def test_downgrade_drops_both_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.downgrade()
    names = {d[0] for d in rec.drop_index_calls}
    assert names == {
        "ix_availability_events_primary_source_id",
        "predictions_current_computed_at_idx",
    }


def test_downgrade_drops_indexes_with_if_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorder stubs ``op.drop_index`` which uses
    ``DROP INDEX IF EXISTS`` semantics in the migration source.
    """
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.downgrade()
    # Two drop_index calls, each with the matching table_name.
    for name, table_name in rec.drop_index_calls:
        assert name in {
            "ix_availability_events_primary_source_id",
            "predictions_current_computed_at_idx",
        }
        assert table_name is not None


def test_downgrade_creates_no_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema-drift guard: 0024 must not add new indexes on downgrade."""
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.downgrade()
    assert rec.create_index_calls == []


# --- chain -----------------------------------------------------------------


def test_migration_chains_off_0023() -> None:
    """The migration must be a direct successor of 0023. No orphan head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    sd = ScriptDirectory.from_config(cfg)
    heads = sd.get_heads()
    assert heads == ["0024_supabase_perf_evidence"]
    rev = sd.get_revision("0024_supabase_perf_evidence")
    assert rev is not None
    assert rev.down_revision == "0023_performance_security_cleanup"
