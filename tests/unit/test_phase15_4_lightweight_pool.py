from __future__ import annotations

import numpy as np

from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.prediction.live_provider import _make_prediction


def test_lightweight_prediction_skips_distribution_sampling() -> None:
    pred = _make_prediction(10, 3, 7.25, expected_minutes=60, start_probability=0.8, source="materialized-chain", data_quality="precomputed-daily-materialize", confidence=0.75, data_completeness=0.85, include_distribution=False)
    assert pred.expected_points == 7.25
    assert pred.distribution.size == 0
    assert pred.floor == 7.25
    assert pred.ceiling == 7.25


def test_full_prediction_still_has_distribution() -> None:
    pred = _make_prediction(10, 3, 7.25, expected_minutes=60, start_probability=0.8, source="materialized-chain", data_quality="precomputed-daily-materialize", confidence=0.75, data_completeness=0.85)
    assert pred.expected_points == 7.25
    assert pred.distribution.size == 2000
    assert np.isfinite(pred.distribution).all()
    assert pred.floor <= pred.ceiling
