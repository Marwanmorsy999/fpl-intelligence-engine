"""Regression coverage for the sync ingestion savepoint retry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

import fpl_intelligence.sync.results_ingestion as results_ingestion


class _Savepoint:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __enter__(self) -> _Savepoint:
        self.calls.append("enter")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.calls.append("rollback" if exc_type is not None else "commit")
        return False


class _Session:
    def __init__(self) -> None:
        self.savepoint_calls: list[str] = []

    def begin_nested(self) -> _Savepoint:
        return _Savepoint(self.savepoint_calls)


def _player_gameweek_integrity_error() -> IntegrityError:
    original = SimpleNamespace(
        diag=SimpleNamespace(constraint_name="uq_player_gameweek")
    )
    return IntegrityError("INSERT", {}, original)


def test_player_gameweek_conflict_retries_inside_second_savepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Session()
    calls = 0

    def _ingest(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _player_gameweek_integrity_error()
        return {"stored": 1, "mirrored": 1}

    monkeypatch.setattr(results_ingestion, "ingest_history_gameweek", _ingest)

    result = results_ingestion._ingest_history_with_race_retry(db, 3, [{"element_id": 1}])

    assert result == {"stored": 1, "mirrored": 1}
    assert calls == 2
    assert db.savepoint_calls == ["enter", "rollback", "enter", "commit"]


def test_unrelated_integrity_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Session()
    error = IntegrityError("INSERT", {}, Exception("uq_unrelated"))
    calls = 0

    def _ingest(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(results_ingestion, "ingest_history_gameweek", _ingest)

    with pytest.raises(IntegrityError) as caught:
        results_ingestion._ingest_history_with_race_retry(db, 3, [{"element_id": 1}])

    assert caught.value is error
    assert calls == 1
    assert db.savepoint_calls == ["enter", "rollback"]
