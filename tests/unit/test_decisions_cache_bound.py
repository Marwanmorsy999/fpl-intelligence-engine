"""Regression tests for the bounded per-session decisions cache."""

from __future__ import annotations

from types import SimpleNamespace

import fpl_intelligence.api.routes.squad as squad_route


def _report(value: int) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def test_decisions_cache_evicts_oldest_entry() -> None:
    original = squad_route._decisions_cache.copy()
    try:
        squad_route._decisions_cache.clear()

        for index in range(squad_route._DECISIONS_CACHE_MAX_ENTRIES):
            squad_route._store_decision_cache(f"session-{index}", _report(index))

        assert len(squad_route._decisions_cache) == squad_route._DECISIONS_CACHE_MAX_ENTRIES
        assert "session-0" in squad_route._decisions_cache

        squad_route._store_decision_cache("session-new", _report(999))

        assert len(squad_route._decisions_cache) == squad_route._DECISIONS_CACHE_MAX_ENTRIES
        assert "session-0" not in squad_route._decisions_cache
        assert squad_route._decisions_cache["session-new"].value == 999
    finally:
        squad_route._decisions_cache.clear()
        squad_route._decisions_cache.update(original)


def test_decisions_cache_store_preserves_existing_key_without_growth() -> None:
    original = squad_route._decisions_cache.copy()
    try:
        squad_route._decisions_cache.clear()
        squad_route._store_decision_cache("same", _report(1))
        squad_route._store_decision_cache("same", _report(2))

        assert len(squad_route._decisions_cache) == 1
        assert squad_route._decisions_cache["same"].value == 2
    finally:
        squad_route._decisions_cache.clear()
        squad_route._decisions_cache.update(original)
