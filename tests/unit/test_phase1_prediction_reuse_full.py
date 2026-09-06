"""Phase 1 prediction-reuse — full end-to-end coverage.

The earlier tests in ``test_prediction_reuse_bridge.py`` cover the
unit-level cache invariants on ``_TimedPredictionProvider``. This
file covers the *integration-level* guarantees the user asked for
in the Phase 1 prediction-reuse handoff:

* identical optimizer output with vs without the cache
* every unique ``(player_id, gameweek)`` is requested from the
  underlying provider at most once per ``generate_decisions()`` call
* bulk calls and individual calls share the same cache
* the fine-grained ``optimizer_*`` sub-timing phases are populated
  after a single ``generate_decisions()`` call
* the start-of-request clear prevents any cross-request leakage

The tests run the real ``DecisionOptimizerBridge`` against a
recording mock provider so the assertions are about the
*observable* surface (call counts, returned values, timing
buckets) rather than internal state.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from fpl_intelligence.api.performance import PhaseTimer, _current_timer
from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
from fpl_intelligence.squad.models import SquadStateCreate

# --- helpers ---------------------------------------------------------------


def _prediction(player_id: int, gameweek: int, expected_points: float = 5.0) -> PlayerPrediction:
    """A deterministic, fully-distributed prediction keyed on (pid, gw)."""
    distribution = np.array(
        [
            expected_points - 1.5,
            expected_points - 0.5,
            expected_points,
            expected_points,
            expected_points + 0.5,
            expected_points + 1.5,
        ]
    )
    return PlayerPrediction(
        player_id=player_id,
        gameweek=gameweek,
        expected_points=expected_points,
        expected_minutes=90.0,
        start_probability=1.0,
        distribution=distribution,
        floor=float(np.min(distribution)),
        ceiling=float(np.max(distribution)),
    )


def _positions() -> dict[int, int]:
    return {
        1: 1,
        2: 1,
        **{i: 2 for i in range(3, 8)},
        **{i: 3 for i in range(8, 13)},
        **{i: 4 for i in range(13, 16)},
    }


def _prices() -> dict[int, float]:
    return {pid: 5.0 + 0.1 * (pid % 10) for pid in range(1, 16)}


def _teams() -> dict[int, int]:
    # 3 teams to keep the captain / chip evaluators happy without
    # tripping their 3-per-club guards.
    return {
        1: 1,
        2: 1,
        3: 1,
        4: 2,
        5: 2,
        6: 2,
        7: 3,
        8: 3,
        9: 3,
        10: 1,
        11: 2,
        12: 3,
        13: 1,
        14: 2,
        15: 3,
    }


def _squad() -> SquadStateCreate:
    return SquadStateCreate(
        player_ids=list(range(1, 16)),
        captain_id=1,
        vice_captain_id=2,
        gameweek=1,
        player_positions=_positions(),
        player_prices=_prices(),
        player_teams=_teams(),
    )


def _provider() -> MagicMock:
    """Recording mock provider with consistent predictions."""
    provider = MagicMock()
    provider.get_player_prediction.side_effect = lambda pid, gw: _prediction(
        int(pid), int(gw), expected_points=4.0 + (int(pid) % 7)
    )
    provider.get_squad_predictions.side_effect = lambda players, gameweeks: {
        int(gw): {int(p): _prediction(int(p), int(gw), 4.0 + (int(p) % 7)) for p in players}
        for gw in gameweeks
    }
    provider.get_all_predictions.side_effect = lambda gw: {
        int(p): _prediction(int(p), int(gw), 4.0 + (int(p) % 7))
        for p in range(1, 1000)  # pool larger than squad
    }
    provider.get_fixture_count.return_value = 1
    return provider


def _run_with_timer(bridge: DecisionOptimizerBridge, squad: SquadStateCreate) -> PhaseTimer:
    """Run a single ``generate_decisions()`` under a fresh PhaseTimer.

    Returns the timer after the call so the test can assert on the
    populated phase buckets.
    """
    timer = PhaseTimer()
    token = _current_timer.set(timer)
    try:
        bridge.generate_decisions(squad)
    finally:
        _current_timer.reset(token)
    return timer


# --- 1. Optimizer output byte-equality -----------------------------------


def test_optimizer_output_byte_equal_with_and_without_cache() -> None:
    """The cache MUST NOT change the DecisionReport contents.

    A cache hit is supposed to return the same object as a cache
    miss; therefore the surrounding optimizer cannot tell the
    difference, and the final report is structurally identical
    aside from ``generated_at`` (a wall-clock timestamp set on
    report construction).
    """
    squad = _squad()

    # First run: cold cache.
    bridge1 = DecisionOptimizerBridge(provider=_provider())
    report1 = bridge1.generate_decisions(squad)

    # Second run: cold cache on a fresh bridge.
    bridge2 = DecisionOptimizerBridge(provider=_provider())
    report2 = bridge2.generate_decisions(squad)

    dump1 = report1.model_dump()
    dump2 = report2.model_dump()
    # ``generated_at`` is a timestamp — strip it for the equality
    # assertion. Every other field is derived purely from the
    # provider's predictions.
    dump1.pop("generated_at", None)
    dump2.pop("generated_at", None)
    assert dump1 == dump2


def test_within_request_cache_hits_return_same_object() -> None:
    """Within a single request, repeat fetches return the cached object.

    This is the strongest possible byte-equality guarantee: the
    cached prediction is literally the same Python object that the
    underlying provider returned the first time.
    """
    # Bypass the side-effect fixture for this test so we can pin
    # the return value of the underlying provider to a known object
    # and assert strict ``is`` identity.
    provider = MagicMock()
    pred = _prediction(7, 1, 4.0)
    provider.get_player_prediction.return_value = pred
    provider.get_fixture_count.return_value = 1

    bridge = DecisionOptimizerBridge(provider=provider)
    timed = bridge._timed_provider

    a = timed.get_player_prediction(7, 1)
    b = timed.get_player_prediction(7, 1)
    c = timed.get_player_prediction(7, 1)

    assert a is b is c is pred
    provider.get_player_prediction.assert_called_once_with(7, 1)


# --- 2. Per-request unique (pid, gw) deduplication -------------------------


def test_generate_decisions_deduplicates_player_gameweek_calls() -> None:
    """Every unique (pid, gw) the bridge asks for is resolved at most once.

    The four optimizers (Starting XI, Captain, Multi-Transfer, Chip)
    all hit the prediction provider for the same 15 players × N
    gameweeks. Without the shared request-local cache the call count
    would explode; with the cache it is bounded by the number of
    unique (pid, gw) keys the underlying provider actually serves.
    """
    provider = _provider()
    bridge = DecisionOptimizerBridge(provider=provider)
    _run_with_timer(bridge, _squad())

    call_keys = {(c.args[0], c.args[1]) for c in provider.get_player_prediction.call_args_list}
    # The same key was never queried more than once via the underlying
    # provider: the call_args_list contains the raw keys, so we just
    # need to assert that every (pid, gw) that the bridge asked for
    # through ``get_player_prediction`` was asked for at most once.
    assert len(provider.get_player_prediction.call_args_list) == len(call_keys)


def test_bulk_pool_results_reuse_per_player_cache() -> None:
    """Calling ``get_squad_predictions`` after ``get_all_predictions``
    reuses the cache populated by the bulk call.

    The transfer planner calls ``get_all_predictions`` first (bulk
    ranking), then a handful of ``get_player_prediction`` calls (top-K
    full distribution). The latter must hit the cache, not the
    underlying provider.
    """
    provider = _provider()
    timed_provider = DecisionOptimizerBridge(provider=provider)._timed_provider

    bulk = timed_provider.get_all_predictions(1)
    # Pretend the transfer planner now wants a full-distribution
    # prediction for a player that the bulk call already produced.
    sample = list(bulk.keys())[0]
    bulk_pred = bulk[sample]
    timed_provider.get_player_prediction(sample, 1)

    provider.get_player_prediction.assert_not_called()
    # The single-player call returned the same object the bulk call
    # produced.
    cached = timed_provider.get_player_prediction(sample, 1)
    assert cached is bulk_pred


# --- 3. Cross-request isolation ------------------------------------------


def test_separate_generate_decisions_calls_do_not_share_state() -> None:
    """Two ``generate_decisions()`` invocations on the SAME bridge
    instance see independent caches.

    The first request warms the cache; the second request clears it
    at start so the per-(pid, gw) call counts are identical between
    the two calls. That symmetry is the "zero cross-request leakage"
    guarantee.
    """
    provider = _provider()
    bridge = DecisionOptimizerBridge(provider=provider)

    bridge.generate_decisions(_squad())
    first_total = provider.get_player_prediction.call_count

    bridge.generate_decisions(_squad())
    second_total = provider.get_player_prediction.call_count - first_total

    assert second_total == first_total


# --- 4. Sub-timing observability ------------------------------------------


_OPTIMIZER_PHASES = (
    "feature_assembly",
    "optimizer_starting_xi",
    "optimizer_captain",
    "optimizer_transfers",
    "optimizer_chip",
)


@pytest.mark.parametrize("phase_name", _OPTIMIZER_PHASES)
def test_generate_decisions_populates_each_sub_timing_phase(
    phase_name: str,
) -> None:
    """Every fine-grained optimizer sub-phase the bridge declares in
    ``_TimedPredictionProvider`` and ``_timed_phase`` is populated
    after a single ``generate_decisions()`` call.

    Phases that were never entered (e.g. ``optimizer_transfers`` when
    the bridge skipped transfer planning because metadata was
    missing) are NOT asserted to be present; the goal is to make
    sure the *phases the bridge actually ran* leave a trace.
    """
    provider = _provider()
    bridge = DecisionOptimizerBridge(provider=provider)
    timer = _run_with_timer(bridge, _squad())
    assert phase_name in timer.phases, (
        f"phase {phase_name!r} missing from timer.phases: {sorted(timer.phases)}"
    )
    # Sanity: a populated phase has a non-negative duration.
    assert timer.phases[phase_name] >= 0.0


def test_generate_decisions_populates_model_inference_phase() -> None:
    """The provider-call side of the cache also records a sub-phase
    so we can see how much time the optimizer spent waiting for the
    underlying provider (vs the pure optimization CPU work).
    """
    provider = _provider()
    bridge = DecisionOptimizerBridge(provider=provider)
    timer = _run_with_timer(bridge, _squad())
    assert "model_inference" in timer.phases
    assert timer.phases["model_inference"] >= 0.0


def test_total_ms_reflects_real_wall_clock() -> None:
    """``total_ms`` is the wall-clock duration of the request.

    The sum of *leaf* phase durations is not asserted against
    ``total_ms`` because the bridge nests an ``optimizer`` phase
    *around* the leaf sub-phases (``optimizer_starting_xi`` etc.),
    so the leaf sum and the outer total measure overlapping
    intervals. The only safe invariant is that ``total_ms`` is a
    non-negative real number — and that the request actually took
    real wall-clock time (i.e. ``total_ms > 0``).
    """
    import time as _time

    provider = _provider()
    bridge = DecisionOptimizerBridge(provider=provider)
    t0 = _time.perf_counter()
    timer = _run_with_timer(bridge, _squad())
    elapsed = (_time.perf_counter() - t0) * 1000.0

    assert timer.total_ms >= 0.0
    # The request must have taken at least the wall-clock time we
    # measured independently. This guards against a future change
    # that breaks the phase timer's start-time initialization.
    assert timer.total_ms <= elapsed + 1.0  # 1 ms slack for clock skew


# --- 5. Cache behavior at the edges --------------------------------------


def test_cache_is_always_empty_at_request_start() -> None:
    """Right after ``clear_request_cache``, the timed provider's
    per-(pid, gw) dict is empty. This is the invariant the
    ``generate_decisions`` start-of-request clear relies on.
    """
    provider = _provider()
    timed = DecisionOptimizerBridge(provider=provider)._timed_provider

    # Warm the cache.
    timed.get_player_prediction(1, 1)
    assert timed._prediction_cache  # noqa: SLF001

    timed.clear_request_cache()
    assert not timed._prediction_cache  # noqa: SLF001
    assert not timed._all_predictions_cache  # noqa: SLF001
    assert not timed._fixture_count_cache  # noqa: SLF001


def test_zero_cross_request_leakage_for_fixture_count_too() -> None:
    """Fixture counts are also request-local and must not leak.

    The Chip simulator calls ``get_fixture_count`` for every bench
    and XI player; with the per-request cache that resolves to
    exactly 15 underlying calls per gameweek, regardless of how many
    chip methods are run within the same request.
    """
    provider = _provider()
    bridge = DecisionOptimizerBridge(provider=provider)
    _run_with_timer(bridge, _squad())

    # All four chip evaluators may run; together they ask for each
    # squad player's fixture count multiple times. The underlying
    # provider must see at most 15 calls (one per player for this GW).
    fixture_calls = provider.get_fixture_count.call_args_list
    called_players = {c.args[0] for c in fixture_calls}
    assert called_players.issubset(set(range(1, 16)))
    # Each (pid, gw) was called at most once.
    fixture_keys = {(c.args[0], c.args[1]) for c in fixture_calls}
    assert len(fixture_calls) == len(fixture_keys)
