"""Regression tests for request-local fixture-count reuse."""

from __future__ import annotations

from unittest.mock import MagicMock

from fpl_intelligence.squad.bridge import DecisionOptimizerBridge


def test_timed_provider_reuses_fixture_count_for_same_player_gameweek() -> None:
    provider = MagicMock()
    provider.get_fixture_count.return_value = 2

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    first = timed.get_fixture_count(123, 5)
    second = timed.get_fixture_count(123, 5)

    assert first == 2
    assert second == 2
    provider.get_fixture_count.assert_called_once_with(123, 5)


def test_fixture_count_cache_is_gameweek_and_player_scoped() -> None:
    provider = MagicMock()
    provider.get_fixture_count.side_effect = [2, 0, 1]

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    player_gw5 = timed.get_fixture_count(123, 5)
    player_gw6 = timed.get_fixture_count(123, 6)
    other_player_gw5 = timed.get_fixture_count(456, 5)
    player_gw5_again = timed.get_fixture_count(123, 5)

    assert (player_gw5, player_gw6, other_player_gw5, player_gw5_again) == (2, 0, 1, 2)
    assert provider.get_fixture_count.call_count == 3


def test_request_cache_clears_fixture_counts_between_generate_decisions() -> None:
    provider = MagicMock()
    provider.get_fixture_count.return_value = 1
    bridge = DecisionOptimizerBridge(provider=provider)

    bridge._timed_provider.get_fixture_count(123, 5)
    bridge._timed_provider.get_fixture_count(123, 5)
    assert provider.get_fixture_count.call_count == 1

    bridge._timed_provider.clear_request_cache()
    bridge._timed_provider.get_fixture_count(123, 5)

    assert provider.get_fixture_count.call_count == 2
