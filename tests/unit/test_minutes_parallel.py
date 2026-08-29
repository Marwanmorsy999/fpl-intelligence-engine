import numpy as np

from fpl_intelligence.prediction.minutes import MinutesModel
from fpl_intelligence.prediction.minutes_parallel import ParallelMinutesModel


def _dataset() -> tuple[dict[int, dict[str, float]], dict[int, float]]:
    features = {
        index: {
            "minutes_last_3": float((index * 7) % 271),
            "minutes_last_5": float((index * 11) % 451),
            "minutes_last_10": float((index * 17) % 901),
            "starts_last_3": float(index % 4),
            "starts_last_5": float(index % 6),
            "starts_last_10": float(index % 11),
            "minutes_prev_match": float((index * 13) % 91),
            "points_prev_match": float(index % 16),
            "points_last_3": float((index * 3) % 31),
            "points_last_5": float((index * 5) % 51),
            "points_last_10": float((index * 9) % 101),
            "goals_last_3": float(index % 3),
            "assists_last_3": float(index % 4),
            "points_per_90": float((index * 2) % 18),
            "n_season_matches": float(index + 1),
        }
        for index in range(60)
    }
    targets = {
        index: float(0 if index % 5 == 0 else min(90, 20 + (index * 9) % 71))
        for index in range(60)
    }
    return features, targets


def test_parallel_fit_matches_sequential_predictions() -> None:
    features, targets = _dataset()
    sequential = MinutesModel(feature_version="2.0.0")
    parallel = ParallelMinutesModel(feature_version="2.0.0")
    sequential.fit(features, targets, {"target": "minutes"})
    parallel.fit(features, targets, {"target": "minutes"})

    probe = [features[index] for index in range(60)]
    sequential_predictions = sequential.predict(probe)
    parallel_predictions = parallel.predict(probe)

    assert sequential_predictions == parallel_predictions
    for name in ("appeared", "started", "60_plus", "90_plus"):
        assert np.allclose(
            sequential._models[name].predict_proba(np.asarray([sequential._vectorize(row) for row in probe]))[:, 1],
            parallel._models[name].predict_proba(np.asarray([parallel._vectorize(row) for row in probe]))[:, 1],
            rtol=0,
            atol=1e-12,
        )
