"""Tests for the Phase 7 PIT shadow-mode harness.

Phase 7 is BLOCKED — INSUFFICIENT HISTORICAL AVAILABILITY DATA.
These tests cover the shadow harness infrastructure so it is ready
when a real pre-deadline availability source is wired.
"""

from __future__ import annotations

import json

from fpl_intelligence.availability.historical.shadow import (
    InMemoryShadowRecorder,
    PITDivergence,
    PITShadowReport,
    ShadowMode,
    compare_predictions,
    gate_can_activate_pit,
    render_report_json,
)

# ---------------------------------------------------------------------------
# Divergence model
# ---------------------------------------------------------------------------


def test_material_delta_threshold_xpcts() -> None:
    d = PITDivergence(
        player_id=1,
        gameweek=28,
        baseline_xpcts=6.0,
        pit_xpcts=7.0,
        delta_xpcts=1.0,
        baseline_minutes=90.0,
        pit_minutes=90.0,
        delta_minutes=0.0,
    )
    assert d.material_delta is True


def test_material_delta_threshold_minutes() -> None:
    d = PITDivergence(
        player_id=2,
        gameweek=28,
        baseline_xpcts=6.0,
        pit_xpcts=6.5,
        delta_xpcts=0.5,
        baseline_minutes=90.0,
        pit_minutes=60.0,
        delta_minutes=-30.0,
    )
    assert d.material_delta is True


def test_material_delta_false_for_small_changes() -> None:
    d = PITDivergence(
        player_id=3,
        gameweek=28,
        baseline_xpcts=6.0,
        pit_xpcts=6.5,
        delta_xpcts=0.5,
        baseline_minutes=90.0,
        pit_minutes=88.0,
        delta_minutes=-2.0,
    )
    assert d.material_delta is False


# ---------------------------------------------------------------------------
# Compare predictions
# ---------------------------------------------------------------------------


def test_compare_predictions_with_pit_returns_divergences() -> None:
    report = compare_predictions(
        entry_id="2295006",
        gameweek=28,
        baseline_predictions={1: {"expected_points": 6.0, "expected_minutes": 90.0}},
        pit_predictions={1: {"expected_points": 7.5, "expected_minutes": 75.0}},
        baseline_xi=[1, 2, 3],
        pit_xi=[1, 2, 3],
        baseline_captain=1,
        pit_captain=1,
    )
    assert report.n_material == 1
    assert report.xi_changed() is False
    assert report.captain_changed() is False


def test_compare_predictions_with_no_pit_returns_empty_divergences() -> None:
    report = compare_predictions(
        entry_id="2295006",
        gameweek=28,
        baseline_predictions={1: {"expected_points": 6.0, "expected_minutes": 90.0}},
        pit_predictions=None,
        baseline_xi=[1, 2, 3],
        pit_xi=[1, 2, 3],
        baseline_captain=1,
        pit_captain=1,
    )
    assert report.divergences == []
    assert report.n_material == 0


def test_compare_predictions_detects_captain_flip() -> None:
    report = compare_predictions(
        entry_id="2295006",
        gameweek=28,
        baseline_predictions={
            1: {"expected_points": 6.0, "expected_minutes": 90.0},
            2: {"expected_points": 8.0, "expected_minutes": 90.0},
        },
        pit_predictions={
            1: {"expected_points": 5.0, "expected_minutes": 90.0},
            2: {"expected_points": 9.0, "expected_minutes": 90.0},
        },
        baseline_xi=[1, 2],
        pit_xi=[1, 2],
        baseline_captain=1,
        pit_captain=2,
    )
    assert report.captain_changed() is True


def test_compare_predictions_detects_xi_change() -> None:
    report = compare_predictions(
        entry_id="2295006",
        gameweek=28,
        baseline_predictions={1: {}, 2: {}, 3: {}},
        pit_predictions={1: {}, 2: {}, 3: {}},
        baseline_xi=[1, 2, 3],
        pit_xi=[3, 2, 1],
        baseline_captain=1,
        pit_captain=3,
    )
    assert report.xi_changed() is False  # sorted equal
    assert report.captain_changed() is True


# ---------------------------------------------------------------------------
# Activation gate
# ---------------------------------------------------------------------------


def test_gate_disabled_when_shadow_mode_not_evaluation_or_live() -> None:
    report = PITShadowReport(
        entry_id="2295006",
        gameweek=28,
        divergences=[
            PITDivergence(
                player_id=1,
                gameweek=28,
                baseline_xpcts=6.0,
                pit_xpcts=6.5,
                delta_xpcts=0.5,
                baseline_minutes=90.0,
                pit_minutes=90.0,
                delta_minutes=0.0,
            ),
        ],
        shadow_mode=ShadowMode.SHADOW,
    )
    assert gate_can_activate_pit(report) is False


def test_gate_disabled_when_captain_flips() -> None:
    report = PITShadowReport(
        entry_id="2295006",
        gameweek=28,
        divergences=[
            PITDivergence(
                player_id=1,
                gameweek=28,
                baseline_xpcts=6.0,
                pit_xpcts=6.5,
                delta_xpcts=0.5,
                baseline_minutes=90.0,
                pit_minutes=90.0,
                delta_minutes=0.0,
            ),
        ],
        baseline_captain=1,
        pit_captain=2,
        shadow_mode=ShadowMode.EVALUATION,
    )
    assert gate_can_activate_pit(report) is False


def test_gate_disabled_with_no_divergences() -> None:
    report = PITShadowReport(
        entry_id="2295006",
        gameweek=28,
        divergences=[],
        shadow_mode=ShadowMode.EVALUATION,
    )
    assert gate_can_activate_pit(report) is False


def test_gate_enabled_when_below_threshold() -> None:
    """<20% material divergences + no captain flip + EVALUATION = PASS."""
    divergences = [
        PITDivergence(
            player_id=i,
            gameweek=28,
            baseline_xpcts=6.0,
            pit_xpcts=6.5,
            delta_xpcts=0.5,
            baseline_minutes=90.0,
            pit_minutes=90.0,
            delta_minutes=0.0,
        )
        for i in range(1, 11)
    ]
    report = PITShadowReport(
        entry_id="2295006",
        gameweek=28,
        divergences=divergences,
        baseline_captain=1,
        pit_captain=1,
        shadow_mode=ShadowMode.EVALUATION,
    )
    assert gate_can_activate_pit(report) is True


def test_gate_disabled_when_too_many_material_changes() -> None:
    divergences = [
        PITDivergence(
            player_id=i,
            gameweek=28,
            baseline_xpcts=6.0,
            pit_xpcts=8.0,
            delta_xpcts=2.0,
            baseline_minutes=90.0,
            pit_minutes=60.0,
            delta_minutes=-30.0,
        )
        for i in range(1, 11)
    ]
    report = PITShadowReport(
        entry_id="2295006",
        gameweek=28,
        divergences=divergences,
        baseline_captain=1,
        pit_captain=1,
        shadow_mode=ShadowMode.EVALUATION,
    )
    assert gate_can_activate_pit(report) is False


# ---------------------------------------------------------------------------
# Recorder + JSON serialization
# ---------------------------------------------------------------------------


def test_in_memory_recorder_accumulates_reports() -> None:
    rec = InMemoryShadowRecorder()
    r1 = PITShadowReport(entry_id="2295006", gameweek=28)
    rec.record(r1)
    rec.record(r1)
    assert len(rec.reports) == 2


def test_render_report_json_is_serializable() -> None:
    report = PITShadowReport(
        entry_id="2295006",
        gameweek=28,
        divergences=[
            PITDivergence(
                player_id=1,
                gameweek=28,
                baseline_xpcts=6.0,
                pit_xpcts=7.0,
                delta_xpcts=1.0,
                baseline_minutes=90.0,
                pit_minutes=90.0,
                delta_minutes=0.0,
            )
        ],
        baseline_captain=1,
        pit_captain=1,
    )
    text = render_report_json(report)
    parsed = json.loads(text)
    assert parsed["entry_id"] == "2295006"
    assert parsed["n_material"] == 1
    assert parsed["captain_changed"] is False
