"""Phase 15.3 — LivePredictionProvider per-instance chain cache.

Verifies the per-gameweek cache in :meth:`LivePredictionProvider.resolve_chain`
eliminates the N+1 redundancy that caused 504 timeouts on /api/v1/decisions.

The cache must:
* Return the cached result on repeated calls for the same gameweek.
* Re-run the chain for a different gameweek (cache miss).
* Be per-instance — a fresh provider has an empty cache.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from fpl_intelligence.prediction.live_provider import (
    LivePredictionProvider,
    PredictionChainResult,
)


class TestChainCacheBehavior:
    """Per-instance _chain_cache eliminates redundant resolve_chain calls."""

    def test_cache_hit_same_gameweek(self, db_session: Session) -> None:
        """Two calls for the same gameweek return the same cached object."""
        provider = LivePredictionProvider(session=db_session)

        result1 = provider.resolve_chain(1)
        result2 = provider.resolve_chain(1)

        assert result1 is result2
        assert 1 in provider._chain_cache

    def test_cache_miss_different_gameweek(self, db_session: Session) -> None:
        """Calls for different gameweeks each run the chain."""
        provider = LivePredictionProvider(session=db_session)

        result1 = provider.resolve_chain(1)
        result2 = provider.resolve_chain(2)

        assert result1 is not result2
        assert 1 in provider._chain_cache
        assert 2 in provider._chain_cache

    def test_cache_per_instance(self, db_session: Session) -> None:
        """A fresh provider has an empty cache — no cross-request leakage."""
        provider1 = LivePredictionProvider(session=db_session)
        provider1.resolve_chain(1)
        assert 1 in provider1._chain_cache

        provider2 = LivePredictionProvider(session=db_session)
        assert 0 == len(provider2._chain_cache)

    def test_cache_reduces_redundant_work(
        self, db_session: Session
    ) -> None:
        """Multiple get_player_prediction calls for same GW return cached results."""
        provider = LivePredictionProvider(session=db_session)

        pred1 = provider.get_player_prediction(1, 1)
        pred2 = provider.get_player_prediction(2, 1)
        pred3 = provider.get_player_prediction(3, 1)

        assert pred1 is not None
        assert pred2 is not None
        assert pred3 is not None
        # All three served from a single chain resolution (one GW cached)
        assert 1 == len(provider._chain_cache)

    def test_cache_identity_same_gameweek(
        self, db_session: Session
    ) -> None:
        """get_player_prediction for same GW returns results from identical chain."""
        provider = LivePredictionProvider(session=db_session)

        # Resolve twice for the same GW — must be the exact same object
        provider.resolve_chain(1)
        first_cached = provider._chain_cache[1]

        provider.get_player_prediction(99, 1)
        second_cached = provider._chain_cache[1]

        assert first_cached is second_cached

    def test_cache_stores_result_after_resolution(
        self, db_session: Session
    ) -> None:
        """After resolve_chain, the result is stored in _chain_cache."""
        provider = LivePredictionProvider(session=db_session)
        assert 0 == len(provider._chain_cache)

        result = provider.resolve_chain(5)

        assert 1 == len(provider._chain_cache)
        assert provider._chain_cache[5] is result
