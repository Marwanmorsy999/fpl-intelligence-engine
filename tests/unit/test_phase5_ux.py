"""Tests for Phase 5 UX/reliability backend modules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_intelligence.api.deep_link import (
    InvalidDeepLink,
    build_deep_link,
    parse_entry,
)
from fpl_intelligence.api.markdown_export import render_decision_markdown
from fpl_intelligence.api.ownership_xpts import (
    compute_row,
    minutes_risk_flag,
    rank_differentials,
)
from fpl_intelligence.api.report_metadata import build_metadata

# ---------------------------------------------------------------------------
# Deep links
# ---------------------------------------------------------------------------


def test_parse_entry_returns_none_when_missing() -> None:
    assert parse_entry("") is None
    assert parse_entry(None) is None  # type: ignore[arg-type]


def test_parse_entry_returns_numeric_string() -> None:
    assert parse_entry("entry=2295006") == "2295006"
    assert parse_entry("entry=2295006&foo=bar") == "2295006"


def test_parse_entry_rejects_non_numeric() -> None:
    with pytest.raises(InvalidDeepLink):
        parse_entry("entry=abc")


def test_parse_entry_rejects_overlong() -> None:
    with pytest.raises(InvalidDeepLink):
        parse_entry("entry=12345678901")


def test_parse_entry_accepts_demo_sentinel() -> None:
    assert parse_entry("entry=demo") == "demo"


def test_build_deep_link_round_trip() -> None:
    url = build_deep_link("https://fpl-intelligence-engine-foundation.vercel.app/", 2295006)
    assert "entry=2295006" in url
    assert parse_entry(url.split("?", 1)[1] if "?" in url else "") == "2295006"


def test_build_deep_link_for_demo() -> None:
    url = build_deep_link("https://fpl-intelligence-engine-foundation.vercel.app/", None)
    assert "entry=" not in url


# ---------------------------------------------------------------------------
# Ownership vs xPTS
# ---------------------------------------------------------------------------


def test_compute_row_basic() -> None:
    r = compute_row(player_id=1, name="Haaland", ownership_pct=60.0, xpcts=8.0)
    assert r.xpcts_per_ownership == pytest.approx(8.0 / 60.0)
    assert r.differential is False  # owned by majority


def test_compute_row_flags_differential() -> None:
    r = compute_row(player_id=2, name="Differential", ownership_pct=5.0, xpcts=6.0)
    assert r.differential is True


def test_compute_row_does_not_flag_low_xpcts_low_ownership() -> None:
    r = compute_row(player_id=3, name="Bench", ownership_pct=1.0, xpcts=2.0)
    assert r.differential is False  # low xPTS


def test_compute_row_handles_zero_ownership_safely() -> None:
    r = compute_row(player_id=4, name="Ghost", ownership_pct=0.0, xpcts=8.0)
    assert r.xpcts_per_ownership == 0.0


def test_rank_differentials_orders_by_xpcts_per_own() -> None:
    rows = [
        compute_row(1, "A", ownership_pct=5.0, xpcts=4.0),  # diff, 0.8/own
        compute_row(2, "B", ownership_pct=10.0, xpcts=8.0),  # diff, 0.8/own, higher xPTS
        compute_row(3, "C", ownership_pct=50.0, xpcts=10.0),  # not diff
    ]
    ranked = rank_differentials(rows, limit=10)
    assert len(ranked) == 2
    # B has same per_own as A but higher xPTS -> B first
    assert ranked[0].player_id == 2
    assert ranked[1].player_id == 1


def test_rank_differentials_respects_limit() -> None:
    rows = [compute_row(i, f"P{i}", ownership_pct=5.0, xpcts=5.0) for i in range(1, 6)]
    assert len(rank_differentials(rows, limit=3)) == 3


def test_minutes_risk_flag_default_threshold() -> None:
    assert minutes_risk_flag(80.0) is False
    assert minutes_risk_flag(30.0) is True


def test_minutes_risk_flag_custom_threshold() -> None:
    assert minutes_risk_flag(70.0, threshold=60.0) is False
    assert minutes_risk_flag(50.0, threshold=60.0) is True


# ---------------------------------------------------------------------------
# Report metadata
# ---------------------------------------------------------------------------


def test_build_metadata_computes_next_allowed_window() -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    meta = build_metadata(
        model_version="v2.7.12",
        data_snapshot_id="20260902T120000Z",
        generated_at=now,
        refresh_state="fresh",
        refresh_window_seconds=900.0,
    )
    assert meta.model_version == "v2.7.12"
    assert meta.refresh_state == "fresh"
    assert meta.refresh_window_seconds == 900.0
    expected_next = (now + timedelta(seconds=900)).isoformat()
    assert meta.next_manual_refresh_allowed_at == expected_next


def test_build_metadata_to_dict_includes_all_fields() -> None:
    now = datetime.now(tz=UTC)
    meta = build_metadata(
        model_version="v1",
        data_snapshot_id="d1",
        generated_at=now,
        refresh_state="fresh",
        refresh_window_seconds=60.0,
    )
    d = meta.to_dict()
    assert set(d) == {
        "model_version",
        "data_snapshot_id",
        "generated_at",
        "refresh_state",
        "refresh_window_seconds",
        "next_manual_refresh_allowed_at",
    }


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------


_REPORT = {
    "entry_id": 2295006,
    "gameweek": 28,
    "starting_xi": [
        {"player_id": 1, "name": "Haaland", "position": "FWD", "expected_points": 8.5},
        {"player_id": 2, "name": "Saka", "position": "MID", "expected_points": 6.2},
    ],
    "bench_order": [
        {"player_id": 12, "name": "Bench1", "position": "DEF", "expected_points": 2.1},
    ],
    "captain": 1,
    "vice_captain": 2,
    "transfers": [
        {
            "out": {"player_id": 5, "name": "Out", "expected_points": 3.0},
            "in": {"player_id": 9, "name": "In", "expected_points": 5.0},
        }
    ],
    "chip_recommendation": {
        "name": "bench_boost",
        "expected_value": 4.5,
        "rationale": "Strong bench this week.",
    },
    "_meta": {
        "model_version": "v2.7.12",
        "data_snapshot_id": "d1",
        "built_at": 1_700_000_000.0,
    },
    "refresh_state": "fresh",
}


def test_markdown_export_includes_required_sections() -> None:
    md = render_decision_markdown(_REPORT)
    assert "# FPL Decision Report" in md
    assert "## Starting XI" in md
    assert "## Bench" in md
    assert "## Captaincy" in md
    assert "## Transfer Recommendations" in md
    assert "## Chip Recommendation" in md
    assert "Haaland" in md
    assert "Saka" in md
    assert "bench_boost" in md


def test_markdown_export_marks_captain() -> None:
    md = render_decision_markdown(_REPORT)
    # Captain row in XI table must include ✓
    assert "| 1 | Haaland | FWD | 8.50 | ✓ |" in md


def test_markdown_export_is_stable_for_same_input() -> None:
    md1 = render_decision_markdown(_REPORT)
    md2 = render_decision_markdown(_REPORT)
    assert md1 == md2


def test_markdown_export_omits_missing_sections() -> None:
    report = {
        "entry_id": 2295006,
        "gameweek": 28,
        "starting_xi": _REPORT["starting_xi"],
        "bench_order": [],
        "captain": 1,
        "vice_captain": 2,
    }
    md = render_decision_markdown(report)
    assert "## Bench" not in md
    assert "## Transfer" not in md
    assert "## Chip" not in md


def test_markdown_export_contains_no_session_or_user_secrets() -> None:
    md = render_decision_markdown(_REPORT)
    # No session id leaks into the export
    assert "session_id" not in md
    # No raw payloads
    assert "_meta" not in md or "raw" not in md
