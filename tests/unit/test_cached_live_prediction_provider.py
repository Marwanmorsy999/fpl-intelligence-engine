from unittest.mock import Mock

import pytest

from fpl_intelligence.prediction.cached_live_provider import CachedLivePredictionProvider


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> CachedLivePredictionProvider:
    instance = CachedLivePredictionProvider.__new__(CachedLivePredictionProvider)
    instance._all_predictions_cache = {}
    instance.session = Mock()

    base_get_all = Mock(
        side_effect=lambda gameweek, *, skip_materialized=False: {
            101: Mock(
                expected_points=7.0 + gameweek,
                expected_minutes=72.0,
                start_probability=0.9,
            )
        }
    )
    monkeypatch.setattr(
        "fpl_intelligence.prediction.cached_live_provider.LivePredictionProvider.get_all_predictions",
        base_get_all,
    )
    instance._base_get_all_mock = base_get_all
    return instance


def test_same_gameweek_builds_full_pool_once(provider: CachedLivePredictionProvider) -> None:
    first = provider.get_all_predictions(3)
    second = provider.get_all_predictions(3)

    assert first is second
    assert first[101].expected_points == 10.0
    provider._base_get_all_mock.assert_called_once_with(3, skip_materialized=False)


def test_different_gameweeks_are_independent(provider: CachedLivePredictionProvider) -> None:
    gw3 = provider.get_all_predictions(3)
    gw4 = provider.get_all_predictions(4)
    gw3_again = provider.get_all_predictions(3)

    assert gw3[101].expected_points == 10.0
    assert gw4[101].expected_points == 11.0
    assert gw3_again is gw3
    assert provider._base_get_all_mock.call_count == 2


def test_skip_materialized_has_an_independent_cache_entry(
    provider: CachedLivePredictionProvider,
) -> None:
    normal = provider.get_all_predictions(3)
    inline = provider.get_all_predictions(3, skip_materialized=True)
    inline_again = provider.get_all_predictions(3, skip_materialized=True)

    assert normal is not inline
    assert inline_again is inline
    assert provider._base_get_all_mock.call_count == 2
    assert provider._base_get_all_mock.call_args_list[0].kwargs == {"skip_materialized": False}
    assert provider._base_get_all_mock.call_args_list[1].kwargs == {"skip_materialized": True}


def test_cached_pool_preserves_prediction_values(provider: CachedLivePredictionProvider) -> None:
    first = provider.get_all_predictions(5)
    second = provider.get_all_predictions(5)
    prediction = second[101]

    assert second is first
    assert prediction.expected_points == 12.0
    assert prediction.expected_minutes == 72.0
    assert prediction.start_probability == 0.9
