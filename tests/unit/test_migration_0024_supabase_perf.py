"""Validate migration 0024 (supabase_perf_evidence) DDL contract."""

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
    """Record the Alembic operations emitted by migration 0024."""

    def __init__(self) -> None:
        self.execute_sql: list[str] = []
        self.create_index_calls: list[tuple[str, str, list[str], bool]] = []
        self.drop_index_calls: list[tuple[str, str | None]] = []

    def execute(self, sql: Any) -> None:
        self.execute_sql.append(str(sql))

    def create_index(self, name: str, table: str, columns: list[str], **kw: Any) -> None:
        self.create_index_calls.append((name, table, list(columns), bool(kw.get("unique", False))))

    def drop_index(self, name: str, *, table_name: str | None = None, **kw: Any) -> None:
        self.drop_index_calls.append((name, table_name))

    def create_table(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
        raise NotImplementedError("0024 must not create tables")

    def drop_table(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
        raise NotImplementedError("0024 must not drop tables")


def _norm(sql: str) -> str:
    return " ".join(sql.split()).upper()


def _execute_calls_normalized(rec: _Recorder) -> list[str]:
    return [_norm(s) for s in rec.execute_sql]


def test_migration_metadata() -> None:
    mig = _load_migration()
    assert mig.revision == "0024_supabase_perf_evidence"
    assert mig.down_revision == "0023_performance_security_cleanup"
    assert mig.branch_labels is None
    assert mig.depends_on is None


def test_upgrade_creates_standalone_unique_index_first(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    creates = [s for s in rec.execute_sql if s.upper().startswith("CREATE UNIQUE INDEX")]
    assert len(creates) == 1, _execute_calls_normalized(rec)
    create = _norm(creates[0])
    assert "PREDICTIONS_CURRENT_PK_IDX" in create
    assert "PREDICTIONS_CURRENT" in create
    assert '"GAMEWEEK"' in create
    assert '"ELEMENT_ID"' in create


def test_upgrade_drops_existing_unique_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    drops = [
        s
        for s in rec.execute_sql
        if s.upper().startswith("ALTER TABLE") and "DROP CONSTRAINT" in s.upper()
    ]
    assert len(drops) == 1, _execute_calls_normalized(rec)
    drop = _norm(drops[0])
    assert "PREDICTIONS_CURRENT" in drop
    assert "UQ_PRED_CURRENT_GW_ELEMENT" in drop


def test_upgrade_promotes_to_pk_with_using_index(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    add_pk_sqls = [
        s
        for s in rec.execute_sql
        if s.upper().startswith("ALTER TABLE") and "ADD CONSTRAINT" in s.upper()
    ]
    assert len(add_pk_sqls) == 1, _execute_calls_normalized(rec)
    add = _norm(add_pk_sqls[0])
    assert "PREDICTIONS_CURRENT_PKEY" in add
    assert "PRIMARY KEY" in add
    assert "USING INDEX PREDICTIONS_CURRENT_PK_IDX" in add
    assert "UQ_PRED_CURRENT_GW_ELEMENT" not in add


def test_upgrade_step_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    step1_at = next(
        i for i, s in enumerate(rec.execute_sql) if s.upper().startswith("CREATE UNIQUE INDEX")
    )
    step2_at = next(
        i
        for i, s in enumerate(rec.execute_sql)
        if s.upper().startswith("ALTER TABLE") and "DROP CONSTRAINT" in s.upper()
    )
    step3_at = next(
        i
        for i, s in enumerate(rec.execute_sql)
        if s.upper().startswith("ALTER TABLE") and "ADD CONSTRAINT" in s.upper()
    )
    assert step1_at < step2_at < step3_at


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


def test_upgrade_creates_exactly_two_perf_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    assert len(rec.create_index_calls) == 2
    names = {c[0] for c in rec.create_index_calls}
    assert names == {
        "predictions_current_computed_at_idx",
        "ix_availability_events_primary_source_id",
    }
    assert "predictions_current_pk_idx" not in names


def test_upgrade_executes_three_pk_alter_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    pk_swap = [
        s
        for s in rec.execute_sql
        if "PREDICTIONS_CURRENT_PK_IDX" in s.upper()
        or "PREDICTIONS_CURRENT_PKEY" in s.upper()
        or "UQ_PRED_CURRENT_GW_ELEMENT" in s.upper()
    ]
    assert len(pk_swap) == 3, _execute_calls_normalized(rec)


def test_upgrade_does_not_create_or_drop_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    assert all("CREATE TABLE" not in s.upper() for s in rec.execute_sql)
    assert all("DROP TABLE" not in s.upper() for s in rec.execute_sql)


def test_downgrade_drops_pk_constraint_first(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.downgrade()
    drop_pk_sqls = [
        s
        for s in rec.execute_sql
        if "DROP CONSTRAINT" in s.upper() and "PREDICTIONS_CURRENT_PKEY" in s.upper()
    ]
    assert len(drop_pk_sqls) == 1, _execute_calls_normalized(rec)


def test_downgrade_recreates_unique_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.downgrade()
    add_unique_sqls = [
        s
        for s in rec.execute_sql
        if "ADD CONSTRAINT" in s.upper()
        and "UNIQUE" in s.upper()
        and "UQ_PRED_CURRENT_GW_ELEMENT" in s.upper()
    ]
    assert len(add_unique_sqls) == 1, _execute_calls_normalized(rec)
    add = _norm(add_unique_sqls[0])
    assert "PREDICTIONS_CURRENT" in add
    assert '"GAMEWEEK"' in add
    assert '"ELEMENT_ID"' in add


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


def test_downgrade_creates_no_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.downgrade()
    assert rec.create_index_calls == []


def test_migration_chains_off_0023() -> None:
    """0024 remains a direct successor of 0023; 0025 is the current head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    sd = ScriptDirectory.from_config(cfg)
    heads = sd.get_heads()
    assert heads == ["0025_decision_snapshots"]
    rev = sd.get_revision("0024_supabase_perf_evidence")
    assert rev is not None
    assert rev.down_revision == "0023_performance_security_cleanup"
