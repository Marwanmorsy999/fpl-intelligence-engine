from datetime import UTC, datetime

from fpl_intelligence.prediction.minutes_validation import TrainingDataset  # type: ignore[attr-defined]
from fpl_intelligence.prediction.minutes_validation_fast import FastMinutesWalkForwardEvaluator, _PerfRow
from fpl_intelligence.prediction.training import TrainingDataBuilder


def _rows() -> list[_PerfRow]:
    cutoff = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        _PerfRow(10, 1, 60, 6, 1, 0, cutoff, cutoff),
        _PerfRow(10, 2, 30, 3, 0, 1, cutoff, cutoff),
        _PerfRow(10, 3, 90, 10, 2, 0, cutoff, cutoff),
        _PerfRow(10, 4, 75, 8, 0, 1, cutoff, cutoff),
    ]


def test_fast_feature_builder_matches_canonical_builder():
    rows = _rows()
    fast = FastMinutesWalkForwardEvaluator._feature_builder(rows)
    canonical = TrainingDataBuilder._compute_player_features_from_performances(rows)
    assert fast == canonical


def test_fast_feature_builder_preserves_recent_windows():
    features = FastMinutesWalkForwardEvaluator._feature_builder(_rows())
    assert features["minutes_last_3"] == 195.0
    assert features["starts_last_3"] == 3.0
    assert features["points_last_3"] == 21.0
    assert features["n_season_matches"] == 4.0
    assert features["points_per_90"] == round((3 + 10 + 8) / (30 + 90 + 75) * 90, 4)
