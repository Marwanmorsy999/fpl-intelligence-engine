"""Stage 2A expected-minutes model contracts."""

from __future__ import annotations

import numpy as np
import pytest

from fpl_intelligence.prediction.minutes import (
    MinutesModel,
    RecentStartBaseline,
    RollingAverageMinutesBaseline,
    SimpleRecentMinutesBaseline,
)


def _training_data() -> tuple[np.ndarray, np.ndarray]:
    features = np.array([[float(index % 5), float(index % 3)] for index in range(60)])
    minutes = np.array([0, 15, 35, 65, 90] * 12, dtype=float)
    return features, minutes


def test_minutes_model_emits_required_probabilities_and_distribution() -> None:
    features, minutes = _training_data()
    model = MinutesModel()
    model.fit(features, minutes, {"target": "minutes"})

    prediction = model.predict(features[:1], {"cutoff_time": "2024-08-16T12:00:00+00:00"})[0]

    required = {
        "probability_start",
        "probability_appearance",
        "probability_60_plus",
        "probability_90",
        "probability_no_appearance",
        "expected_minutes",
        "uncertainty",
        "distribution",
        "model_version",
        "feature_version",
        "data_version",
        "confidence",
        "reason_codes",
    }
    assert required <= prediction.keys()
    assert abs(sum(prediction["distribution"].values()) - 1.0) < 1e-6
    assert all(
        0.0 <= prediction[key] <= 1.0
        for key in (
            "probability_start",
            "probability_appearance",
            "probability_60_plus",
            "probability_90",
            "probability_no_appearance",
            "confidence",
        )
    )


def test_minutes_model_supports_explicit_started_target() -> None:
    features, minutes = _training_data()
    started = np.array([index % 2 for index in range(len(minutes))])
    model = MinutesModel()
    model.fit(features, minutes, {"started": started})

    prediction = model.predict(features[:1])[0]
    assert "probability_starting" in prediction
    assert prediction["model_version"] == "2.0.0"


def test_expected_minutes_matches_player_specific_distribution() -> None:
    features = np.array([[0.0, 0.0], [10.0, 10.0]] * 30)
    minutes = np.array([0.0, 90.0] * 30)
    model = MinutesModel()
    model.fit(features, minutes)

    predictions = model.predict(features[:2])
    assert predictions[0]["expected_minutes"] != predictions[1]["expected_minutes"]
    for prediction in predictions:
        distribution = prediction["distribution"]
        assert sum(distribution.values()) == pytest.approx(1.0, abs=1e-6)
        assert all(0.0 <= value <= 1.0 for value in distribution.values())
        implied = sum(
            model._bucket_means[bucket] * value for bucket, value in distribution.items()
        )
        assert prediction["expected_minutes"] == pytest.approx(implied, abs=1e-4)
        assert 0.0 <= prediction["expected_minutes"] <= 90.0


def test_bucket_means_use_training_window_only() -> None:
    model = MinutesModel()
    model.fit(np.zeros((4, 2)), np.array([10.0, 20.0, 30.0, 40.0]))

    assert model._bucket_means["1_29"] == pytest.approx(15.0)
    assert model._bucket_means["30_59"] == pytest.approx(35.0)


def test_minutes_baselines_have_common_prediction_contract() -> None:
    feature_batch = {
        1: {
            "minutes_last_3": 180,
            "starts_last_3": 2,
            "minutes_last_10": 600,
            "n_season_matches": 10,
        },
    }
    for baseline in (
        SimpleRecentMinutesBaseline(),
        RecentStartBaseline(),
        RollingAverageMinutesBaseline(),
    ):
        prediction = baseline.predict_batch(feature_batch, cutoff=None)[1]
        assert {
            "expected_minutes",
            "probability_start",
            "probability_60_plus",
            "uncertainty",
        } <= prediction.keys()
