from __future__ import annotations

import pytest

from fpl_intelligence.db import session
from scripts.preflight_minutes_validation import _print_report


def test_validation_database_requires_explicit_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session.settings, "database_url", session._DEFAULT_PG_PLACEHOLDER)

    with pytest.raises(RuntimeError, match="DATABASE_URL is not configured"):
        session.validation_database_url()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql://user:pass@remote.example:5432/fpl",
            "postgresql+psycopg://user:pass@remote.example:5432/fpl",
        ),
        (
            "postgresql+psycopg://user:pass@remote.example:5432/fpl",
            "postgresql+psycopg://user:pass@remote.example:5432/fpl",
        ),
    ],
)
def test_validation_database_normalizes_to_psycopg3(
    monkeypatch: pytest.MonkeyPatch, url: str, expected: str
) -> None:
    monkeypatch.setattr(session.settings, "database_url", url)

    assert session.validation_database_url() == expected


def test_validation_session_uses_psycopg3_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session.settings,
        "database_url",
        "postgresql://user:pass@remote.example:5432/fpl",
    )

    session_factory = session.validation_session_factory()
    try:
        assert session_factory.kw["bind"].dialect.driver == "psycopg"
    finally:
        session_factory.kw["bind"].dispose()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg2://user:pass@remote.example:5432/fpl",
        "sqlite:///./fpl_local.db",
        "mysql://user:pass@host/db",
    ],
)
def test_validation_database_rejects_non_postgres_urls(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(session.settings, "database_url", url)

    with pytest.raises(RuntimeError, match="PostgreSQL|Psycopg 3"):
        session.validation_database_url()


def test_preflight_reports_missing_schema(capsys: pytest.CaptureFixture[str]) -> None:
    result = _print_report({"missing_tables": ["player_gameweek_performances"]})

    assert result == 1
    output = capsys.readouterr().out
    assert "required structures missing" in output
    assert "player_gameweek_performances" in output


def test_preflight_report_contains_coverage_without_url(capsys: pytest.CaptureFixture[str]) -> None:
    report = {
        "missing_tables": [],
        "season_counts": {
            season: {"players": 2, "fixtures": 3, "gameweeks": 4, "performance": 5}
            for season in ("2022-23", "2023-24", "2024-25")
        },
        "total_performance": 15,
        "temporal": {
            "performance_available_at": 15,
            "performance_ingested_at": 15,
            "gameweek_deadline_time": 12,
        },
        "mapping_failures": {"player": 0, "team": 0, "fixture": 0},
        "duplicate_rows": 0,
        "missing_critical_values": 0,
        "invalid_timestamps": 0,
    }

    result = _print_report(report)

    assert result == 0
    output = capsys.readouterr().out
    assert "coverage 2022-23" in output
    assert "entity resolution failures" in output
    assert "postgresql" not in output.lower()


def test_preflight_rejects_invalid_temporal_provenance(capsys: pytest.CaptureFixture[str]) -> None:
    report = {
        "missing_tables": [],
        "season_counts": {
            season: {"players": 2, "fixtures": 3, "gameweeks": 4, "performance": 5}
            for season in ("2022-23", "2023-24", "2024-25")
        },
        "total_performance": 15,
        "temporal": {
            "performance_available_at": 14,
            "performance_ingested_at": 15,
            "gameweek_deadline_time": 12,
        },
        "mapping_failures": {"player": 0, "team": 0, "fixture": 0},
        "duplicate_rows": 0,
        "missing_critical_values": 0,
        "invalid_timestamps": 0,
    }

    assert _print_report(report) == 1
    assert "invalid temporal provenance" in capsys.readouterr().out