"""Validate migration 0024 (supabase_perf_evidence) DDL contract.

Migration 0024 issues only ``op.execute(...)`` (3 statements in
upgrade, 2 in downgrade) and ``op.create_index(...)`` (two new
performance indexes). The PK change is a 3-step process because
PostgreSQL does not allow an index already owned by a UNIQUE
constraint to be reused directly with ``PRIMARY KEY USING INDEX``:

    1. CREATE UNIQUE INDEX predictions_current_pk_idx
       ON public.predictions_current ("gameweek", "element_id")
    2. ALTER TABLE public.predictions_current
       DROP CONSTRAINT uq_pred_current_gw_element
    3. ALTER TABLE public.predictions_current
       ADD CONSTRAINT predictions_current_pkey
       PRIMARY KEY USING INDEX predictions_current_pk_idx

This is the standard Postgres procedure documented in
https://www.postgresql.org/docs/current/sql-altertable.html.

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


# --- helpers ---------------------------------------------------------------


def _norm(sql: str) -> str:
    """Normalize a SQL string for comparison: collapse whitespace, uppercase."""
    return " ".join(sql.split()).upper()


def _execute_calls_normalized(rec: _Recorder) -> list[str]:
    return [_norm(s) for s in rec.execute_sql]


# --- metadata --------------------------------------------------------------


def test_migration_metadata() -> None:
    mig = _load_migration()
    assert mig.revision == "0024_supabase_perf_evidence"
    assert mig.down_revision == "0023_performance_security_cleanup"
    assert mig.branch_labels is None
    assert mig.depends_on is None


# --- upgrade ---------------------------------------------------------------


def test_upgrade_creates_standalone_unique_index_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 1: standalone UNIQUE index on (gameweek, element_id).

    Required because Postgres will not let us promote an index that
    is still owned by the existing UNIQUE constraint.
    """
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


def test_upgrade_drops_existing_unique_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 2: drop the existing UNIQUE constraint.

    This frees the (gameweek, element_id) uniqueness invariant to be
    re-asserted by the new PK in step 3.
    """
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


def test_upgrade_promotes_to_pk_with_using_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 3: add the PK using the new standalone index.

    The crucial contract: the PK must reference the *new* index
    (predictions_current_pk_idx), NOT the original UNIQUE
    (uq_pred_current_gw_element), because the original UNIQUE has
    already been dropped in step 2.
    """
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
    # The original UNIQUE must NOT be referenced.
    assert "UQ_PRED_CURRENT_GW_ELEMENT" not in add


def test_upgrade_step_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 3 PK-alteration steps must occur in the documented order.

    1. CREATE UNIQUE INDEX
    2. ALTER TABLE ... DROP CONSTRAINT
    3. ALTER TABLE ... ADD CONSTRAINT ... PRIMARY KEY USING INDEX
    """
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
    assert step1_at < step2_at < step3_at, (
        f"step order wrong: create={step1_at}, drop={step2_at}, add={step3_at}"
    )


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


def test_upgrade_creates_exactly_two_perf_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two performance indexes (``predictions_current_computed_at_idx``
    and ``ix_availability_events_primary_source_id``) are created via
    ``op.create_index``. The PK-backup index
    ``predictions_current_pk_idx`` is created via raw SQL, so it
    must NOT appear here.
    """
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
    # Drift guard: the PK-backup index must never be created via op.create_index.
    assert "predictions_current_pk_idx" not in names


def test_upgrade_executes_three_pk_alter_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PK swap is a 3-step process: 3 op.execute() calls."""
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    # Filter to the PK-swap SQL only (CREATE UNIQUE + ALTER TABLE).
    pk_swap = [
        s
        for s in rec.execute_sql
        if "PREDICTIONS_CURRENT_PK_IDX" in s.upper()
        or "PREDICTIONS_CURRENT_PKEY" in s.upper()
        or "UQ_PRED_CURRENT_GW_ELEMENT" in s.upper()
    ]
    assert len(pk_swap) == 3, _execute_calls_normalized(rec)


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


def test_downgrade_drops_pk_constraint_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downgrade step 1: DROP CONSTRAINT predictions_current_pkey.

    Postgres drops the backing index automatically.
    """
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


def test_downgrade_recreates_unique_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downgrade step 2: re-add the original UNIQUE constraint.

    This restores the pre-migration invariant on (gameweek, element_id).
    """
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
