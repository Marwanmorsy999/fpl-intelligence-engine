from __future__ import annotations

from datetime import UTC, datetime

from fpl_intelligence.availability.historical.verified_deadlines import load_verified_deadline_cutoffs


def test_verified_catalog_contains_first_five_gameweeks_for_two_seasons() -> None:
    cutoffs = load_verified_deadline_cutoffs(["2024-25", "2025-26"], gw_min=1, gw_max=5)
    assert len(cutoffs) == 10
    assert [(c.season_code, c.gameweek) for c in cutoffs] == [
        ("2024-25", 1),
        ("2024-25", 2),
        ("2024-25", 3),
        ("2024-25", 4),
        ("2024-25", 5),
        ("2025-26", 1),
        ("2025-26", 2),
        ("2025-26", 3),
        ("2025-26", 4),
        ("2025-26", 5),
    ]


def test_verified_catalog_preserves_utc_cutoff() -> None:
    cutoffs = load_verified_deadline_cutoffs(["2024-25"], gw_min=1, gw_max=1)
    assert cutoffs[0].cutoff == datetime(2024, 8, 16, 17, 30, tzinfo=UTC)
    assert cutoffs[0].cutoff.tzinfo is UTC


def test_verified_catalog_returns_empty_for_uncovered_gameweek() -> None:
    assert load_verified_deadline_cutoffs(["2024-25"], gw_min=6, gw_max=10) == []
