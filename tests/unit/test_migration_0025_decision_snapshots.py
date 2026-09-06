"""Validate migration 0025 (decision_snapshots) DDL contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_MIG_PATH = Path("migrations/versions/0025_decision_snapshots.py")


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0025", _MIG_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Recorder:
    def __init__(self) -> None:
        self.tables: list[tuple[str, list[tuple[str, Any]]]] = []
        self.indexes: list[tuple[str, str, list[str]]] = []
        self.drops_table: list[str] = []
        self.drops_index: list[tuple[str, str | None]] = []

    def create_table(self, name: str, *cols: Any, **kw: Any) -> None:
        self.tables.append((name, [c for c in cols]))

    def create_index(self, name: str, table_name: str, columns: list[str], **kw: Any) -> None:
        self.indexes.append((name, table_name, list(columns)))

    def drop_index(self, name: str, *, table_name: str | None = None, **kw: Any) -> None:
        self.drops_index.append((name, table_name))

    def drop_table(self, name: str) -> None:
        self.drops_table.append(name)

    def Column(self, *args: Any, **kw: Any) -> Any:
        return ("Column", args, kw)

    def UniqueConstraint(self, *args: Any, **kw: Any) -> Any:
        return ("UniqueConstraint", args, kw)

    @property
    def sa(self) -> Any:
        import sqlalchemy as sa

        class _SaProxy:
            def __getattr__(self, attr: str) -> Any:
                return getattr(sa, attr)

        return _SaProxy()


def test_migration_metadata() -> None:
    mig = _load_migration()
    assert mig.revision == "0025_decision_snapshots"
    assert mig.down_revision == "0024_supabase_perf_evidence"


def test_upgrade_creates_decision_snapshots_table(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    names = [t[0] for t in rec.tables]
    assert names == ["decision_snapshots"]


def test_upgrade_creates_documented_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.upgrade()
    by_name = {idx[0]: idx for idx in rec.indexes}
    assert "ix_decision_snapshots_session_gameweek" in by_name
    assert "ix_decision_snapshots_generated_at" in by_name


def test_downgrade_reverses_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _load_migration()
    rec = _Recorder()
    monkeypatch.setattr(mig, "op", rec)
    mig.downgrade()
    assert rec.drops_table == ["decision_snapshots"]
    drop_index_names = {d[0] for d in rec.drops_index}
    assert drop_index_names == {
        "ix_decision_snapshots_generated_at",
        "ix_decision_snapshots_session_gameweek",
    }
