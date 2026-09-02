from __future__ import annotations

from datetime import UTC, datetime


def test_decisions_cache_is_bounded_and_evicts_oldest() -> None:
    from fpl_intelligence.api.routes.squad import (
        _DECISIONS_CACHE_MAX_ENTRIES,
        _BoundedDecisionsCache,
    )

    cache = _BoundedDecisionsCache(3)
    for i in range(5):
        cache[f"k{i}"] = i

    assert len(cache) == 3
    assert list(cache.keys()) == ["k2", "k3", "k4"]
    assert cache.get("k0") is None
    assert _DECISIONS_CACHE_MAX_ENTRIES == 256


def test_decisions_cache_replacing_key_keeps_single_entry() -> None:
    from fpl_intelligence.api.routes.squad import _BoundedDecisionsCache

    cache = _BoundedDecisionsCache(2)
    cache["same"] = "first"
    cache["same"] = "second"

    assert len(cache) == 1
    assert cache.get("same") == "second"


def test_session_invalidation_preserves_other_sessions() -> None:
    from fpl_intelligence.api.routes.squad import (
        _decisions_cache,
        _decisions_cache_key,
        _decisions_cache_lock,
        _invalidate_decisions_cache,
    )

    with _decisions_cache_lock:
        _decisions_cache.clear()
        stamp = datetime(2026, 9, 2, tzinfo=UTC)
        _decisions_cache[_decisions_cache_key("A", stamp, 1)] = "a1"
        _decisions_cache[_decisions_cache_key("A", stamp, 2)] = "a2"
        _decisions_cache[_decisions_cache_key("B", stamp, 1)] = "b1"

    _invalidate_decisions_cache("A")

    with _decisions_cache_lock:
        assert all(not key.startswith("A:") for key in _decisions_cache)
        assert any(key.startswith("B:") for key in _decisions_cache)
        _decisions_cache.clear()


def test_cache_key_semantics_keep_updated_at_and_gameweek_distinct() -> None:
    from fpl_intelligence.api.routes.squad import _decisions_cache_key

    stamp1 = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    stamp2 = datetime(2026, 9, 2, 10, 1, tzinfo=UTC)

    assert _decisions_cache_key("A", stamp1, 1) != _decisions_cache_key("A", stamp2, 1)
    assert _decisions_cache_key("A", stamp1, 1) != _decisions_cache_key("A", stamp1, 2)


def test_global_cache_clear_remains_dict_compatible() -> None:
    from fpl_intelligence.api.routes.squad import _decisions_cache, _decisions_cache_lock

    with _decisions_cache_lock:
        _decisions_cache.clear()
        _decisions_cache["x"] = 1
        _decisions_cache["y"] = 2
        assert _decisions_cache.get("x") == 1
        assert set(_decisions_cache.keys()) == {"x", "y"}
        assert _decisions_cache.pop("x") == 1
        _decisions_cache.clear()
        assert len(_decisions_cache) == 0
