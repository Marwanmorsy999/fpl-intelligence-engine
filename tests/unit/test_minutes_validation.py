from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fpl_intelligence.prediction.minutes_validation import (
    ValidationResult,
    ValidationRow,
    blend_prediction,
    calibration_error,
    metric_summary,
    reliability_table,
    render_report,
    select_blend_weight,
    select_conditional_blend_weights,
)

MODELS = ("recent_minutes", "recent_start", "rolling_average", "candidate", "blend")


def _row(player_id: int, minutes: float, probability: float = 0.75) -> ValidationRow:
    prediction = {
        "expected_minutes": 75.0,
        "probability_start": probability,
        "probability_appearance": probability,
        "probability_60_plus": probability,
    }
    return ValidationRow(
        season="2023-24",
        gameweek=4,
        cutoff_time=datetime(2023, 9, 1, tzinfo=UTC),
        player_id=player_id,
        position="MID",
        minutes=minutes,
        predictions={model: {player_id: prediction.copy()} for model in MODELS},
        features={"starts_last_10": 8.0, "minutes_last_10": 720.0},
    )


def test_metric_calculations_include_sample_size() -> None:
    metrics = metric_summary([_row(1, 90), _row(2, 0, 0.25)], "candidate")

    assert metrics["n"] == 2
    assert metrics["mae"] == pytest.approx(45.0)
    assert metrics["rmse"] == pytest.approx(54.0832691)
    assert metrics["start_brier"] == pytest.approx(0.0625)
    assert metrics["start_log_loss"] is not None


def test_empty_and_single_class_metrics_are_explicit() -> None:
    metrics = metric_summary([], "candidate")
    assert metrics["n"] == 0
    assert metrics["mae"] is None
    assert metrics["roc_auc_start"] is None
    assert _row(1, 90) is not None
    assert metric_summary([_row(1, 90)], "candidate")["roc_auc_start"] is None


def test_reliability_has_all_probability_buckets_and_n() -> None:
    table = reliability_table([_row(1, 90), _row(2, 0, 0.25)], "candidate", "start")

    assert len(table) == 10
    assert sum(int(row["n"] or 0) for row in table) == 2
    assert all("predicted_probability" in row and "observed_frequency" in row for row in table)


def test_calibration_error_is_zero_for_reliable_predictions() -> None:
    import numpy as np

    assert calibration_error(np.array([0.0, 1.0]), np.array([0.0, 1.0])) == pytest.approx(0.0)
    assert calibration_error(np.array([]), np.array([])) is None


def test_report_is_deterministic_and_has_promotion_gate() -> None:
    result = ValidationResult(
        [_row(1, 90)], [{"gameweek": 4, "n_predictions": 1}], {}, ["2023-24"]
    )

    first = render_report(result)
    second = render_report(result)
    assert first == second
    assert "## Promotion decision" in first
    assert "N = 1" in first
    assert "evaluated candidate denominator N = 1" in first
    assert "Fold prediction total: N = 1" in first


def test_empty_report_is_insufficient_evidence() -> None:
    report = render_report(ValidationResult([], [], {}, []))

    assert "INSUFFICIENT EVIDENCE" in report
    assert "No canonical rows available." in report


def test_blend_weight_is_deterministic_and_ignores_outer_rows() -> None:
    model = [20.0, 80.0, 20.0, 80.0]
    recent = [40.0, 60.0, 40.0, 60.0]
    inner_actual = [20.0, 80.0]

    first = select_blend_weight(model[:2], recent[:2], inner_actual)
    second = select_blend_weight(model[:2], recent[:2], inner_actual)
    assert first == second == 1.0
    assert select_blend_weight(model[:2], recent[:2], [40.0, 60.0]) == 0.0


def test_conditional_weights_require_sufficient_group_improvement() -> None:
    model = [20.0] * 20 + [80.0] * 20
    recent = [40.0] * 20 + [60.0] * 20
    actual = [20.0] * 20 + [60.0] * 20
    groups = ["GK"] * 20 + ["MID"] * 20

    weights = select_conditional_blend_weights(model, recent, actual, groups)

    assert weights["global"] == 0.0
    assert weights["GK"] == 1.0
    assert "MID" not in weights


def test_blend_preserves_probabilities_and_bounds_expected_minutes() -> None:
    model = {
        "expected_minutes": 120.0,
        "probability_start": 0.7,
        "probability_appearance": 0.8,
        "probability_60_plus": 0.6,
    }
    recent = {"expected_minutes": -20.0}

    blended = blend_prediction(model, recent, 0.5, "train", "inner")

    assert blended["expected_minutes"] == 50.0
    assert blended["probability_start"] == model["probability_start"]
    assert blended["probability_appearance"] == model["probability_appearance"]
    assert blended["probability_60_plus"] == model["probability_60_plus"]
    assert blended["expected_minutes_method"] == "walkforward_blend"
