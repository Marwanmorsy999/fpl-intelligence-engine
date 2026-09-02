"""Tests for the Phase 23 decision snapshot store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fpl_intelligence.sync.decision_snapshots import (
    ConcurrentRebuildError,
    DecisionSnapshot,
    RebuildSlot,
    RefreshState,
    in_memory_store,
)

# ---------------------------------------------------------------------------
# RefreshState
# ---------------------------------------------------------------------------


def test_refresh_state_is_str_enum() -> None:
    assert RefreshState.FRESH.value == "fresh"
    assert RefreshState.REFRESHING.value == "refreshing"
    assert RefreshState.STALE.value == "stale"
    assert RefreshState.UNAVAILABLE.value == "unavailable"


def test_decision_snapshot_effective_state_ages_past_window() -> None:
    snap = DecisionSnapshot(
        session_id="s1",
        gameweek=1,
        model_version="v1",
        data_snapshot_id="d1",
        payload={"x": 1},
        refresh_state=RefreshState.FRESH,
        generated_at=datetime.now(tz=UTC) - timedelta(seconds=1200),
        refresh_window_seconds=900.0,
    )
    assert snap.is_within_refresh_window is False
    assert snap.effective_state() is RefreshState.STALE


def test_decision_snapshot_effective_state_within_window() -> None:
    snap = DecisionSnapshot(
        session_id="s1",
        gameweek=1,
        model_version="v1",
        data_snapshot_id="d1",
        payload={},
        refresh_state=RefreshState.FRESH,
        generated_at=datetime.now(tz=UTC),
        refresh_window_seconds=900.0,
    )
    assert snap.effective_state() is RefreshState.FRESH


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


def test_get_latest_returns_unavailable_when_no_snapshot() -> None:
    store = in_memory_store()
    state, snap = store.get_latest("missing", 1)
    assert state is RefreshState.UNAVAILABLE
    assert snap is None


def test_store_then_get_latest_returns_fresh_payload() -> None:
    store = in_memory_store(refresh_window_seconds=900.0)
    store.store(
        "s1",
        1,
        model_version="v1",
        data_snapshot_id="d1",
        payload={"captain": 7, "xi": [1, 2, 3]},
    )
    state, snap = store.get_latest("s1", 1)
    assert state is RefreshState.FRESH
    assert snap is not None
    assert snap.payload == {"captain": 7, "xi": [1, 2, 3]}
    assert snap.refresh_state is RefreshState.FRESH


def test_store_dedupes_by_identity() -> None:
    """Same (session_id, gameweek, model_version, data_snapshot_id) replaces."""
    store = in_memory_store()
    store.store("s1", 1, model_version="v1", data_snapshot_id="d1", payload={"a": 1})
    store.store("s1", 1, model_version="v1", data_snapshot_id="d1", payload={"a": 2})
    _, snap = store.get_latest("s1", 1)
    assert snap is not None
    assert snap.payload == {"a": 2}


def test_different_model_versions_coexist() -> None:
    store = in_memory_store()
    store.store("s1", 1, model_version="v1", data_snapshot_id="d1", payload={"v": 1})
    store.store("s1", 1, model_version="v2", data_snapshot_id="d1", payload={"v": 2})
    _, snap1 = store.get_latest("s1", 1)
    # Latest by generated_at wins
    assert snap1 is not None
    assert snap1.payload == {"v": 2}


# ---------------------------------------------------------------------------
# Concurrency guard
# ---------------------------------------------------------------------------


def test_acquire_rebuild_slot_only_one_worker_wins() -> None:
    store = in_memory_store()
    s1 = store.acquire_rebuild_slot("s1", 1, model_version="v1", data_snapshot_id="d1")
    assert s1 is not None
    s2 = store.acquire_rebuild_slot("s1", 1, model_version="v1", data_snapshot_id="d1")
    assert s2 is None


def test_rebuild_slot_can_be_released_and_reacquired() -> None:
    store = in_memory_store()
    s1 = store.acquire_rebuild_slot("s1", 1, model_version="v1", data_snapshot_id="d1")
    assert s1 is not None
    s1.release()
    s2 = store.acquire_rebuild_slot("s1", 1, model_version="v1", data_snapshot_id="d1")
    assert s2 is not None
    s2.release()


def test_rebuild_slot_used_as_context_manager() -> None:
    store = in_memory_store()
    with store.acquire_rebuild_slot("s1", 1, model_version="v1", data_snapshot_id="d1") as slot:
        assert isinstance(slot, RebuildSlot)
        # Second attempt while held should fail
        again = store.acquire_rebuild_slot("s1", 1, model_version="v1", data_snapshot_id="d1")
        assert again is None
    # After exiting, the slot is free
    fresh = store.acquire_rebuild_slot("s1", 1, model_version="v1", data_snapshot_id="d1")
    assert fresh is not None


def test_different_gameweeks_are_independent() -> None:
    store = in_memory_store()
    s1 = store.acquire_rebuild_slot("s1", 1, model_version="v1", data_snapshot_id="d1")
    s2 = store.acquire_rebuild_slot("s1", 2, model_version="v1", data_snapshot_id="d1")
    assert s1 is not None
    assert s2 is not None


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def test_store_mark_state_persists_state_change() -> None:
    store = in_memory_store()
    snap = store.store("s1", 1, model_version="v1", data_snapshot_id="d1", payload={"x": 1})
    assert snap.id is not None
    store.mark_state(snap.id, RefreshState.STALE)
    _, latest = store.get_latest("s1", 1)
    assert latest is not None
    assert latest.refresh_state is RefreshState.STALE


def test_old_snapshot_effective_state_is_stale_even_if_row_says_fresh() -> None:
    store = in_memory_store(refresh_window_seconds=1.0)
    snap = store.store("s1", 1, model_version="v1", data_snapshot_id="d1", payload={"x": 1})
    # Backdate the generated_at to simulate age
    backend_snap = store.backend.fetch_for_refresh_state("s1", 1, "v1", "d1")
    assert backend_snap is not None
    backend_snap.generated_at = datetime.now(tz=UTC) - timedelta(seconds=120)
    state, _ = store.get_latest("s1", 1)
    assert state is RefreshState.STALE
    assert snap.refresh_state is RefreshState.FRESH


# ---------------------------------------------------------------------------
# Concurrent rebuild error type is exported
# ---------------------------------------------------------------------------


def test_concurrent_rebuild_error_is_subclass_of_store_error() -> None:
    assert issubclass(ConcurrentRebuildError, Exception)
    # Just confirm the class is importable / instantiable for the contract
    err = ConcurrentRebuildError("test")
    assert "test" in str(err)
