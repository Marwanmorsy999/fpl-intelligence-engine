from __future__ import annotations

from fpl_intelligence.prediction.minutes_validation import blend_prediction


def _prediction(expected_minutes: float, start_probability: float) -> dict[str, object]:
    return {
        "expected_minutes": expected_minutes,
        "probability_starting": start_probability,
        "probability_start": start_probability,
        "method": "logistic",
    }


def test_weight_zero_uses_recent_minutes_but_retains_candidate_start_probability() -> None:
    candidate = _prediction(80.0, 0.82)
    recent = _prediction(40.0, 0.41)

    blended = blend_prediction(
        candidate,
        recent,
        0.0,
        "train-window",
        "validation-window",
    )

    assert blended["expected_minutes"] == 40.0
    assert blended["probability_starting"] == 0.82
    assert blended["probability_start"] == 0.82
    assert blended["expected_minutes_method"] == "walkforward_blend"
    assert blended["expected_minutes_model_weight"] == 0.0


def test_weight_one_uses_candidate_expected_minutes_and_candidate_start_probability() -> None:
    candidate = _prediction(80.0, 0.82)
    recent = _prediction(40.0, 0.41)

    blended = blend_prediction(
        candidate,
        recent,
        1.0,
        "train-window",
        "validation-window",
    )

    assert blended["expected_minutes"] == 80.0
    assert blended["probability_starting"] == 0.82
    assert blended["probability_start"] == 0.82
    assert blended["expected_minutes_model_weight"] == 1.0


def test_intermediate_weight_blends_only_expected_minutes() -> None:
    candidate = _prediction(80.0, 0.82)
    recent = _prediction(40.0, 0.41)

    blended = blend_prediction(
        candidate,
        recent,
        0.25,
        "train-window",
        "validation-window",
    )

    assert blended["expected_minutes"] == 50.0
    assert blended["probability_starting"] == 0.82
    assert blended["probability_start"] == 0.82
