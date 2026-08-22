"""Tests for no-look-ahead leakage prevention.

These tests verify that the feature store and backtesting engine
never use data that was not available at the historical decision cutoff.
They are the most critical tests for ensuring backtest validity.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from fpl_intelligence.db.models import (
    FPLSnapshot,
    Gameweek,
    PlayerGameweekPerformance,
)
from fpl_intelligence.features.calculators.market_features import MarketFeaturesCalculator
from fpl_intelligence.features.calculators.player_form import PlayerFormCalculator
from fpl_intelligence.features.temporal import (
    InformationAccessPolicy,
    TemporalQueryBuilder,
    _ensure_aware,
    is_record_available,
)


class TestNoLookAheadEnforcement:
    """Tests that verify no-look-ahead is enforced at the query level."""

    def test_future_data_excluded_from_query(self, db_session, populated_db) -> None:
        """Test that data with available_at > cutoff is excluded from queries."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        # Add a snapshot that is in the future relative to cutoff
        future_snapshot = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            price=10.0,
            total_points=50,
        )
        db_session.add(future_snapshot)
        db_session.commit()

        builder = TemporalQueryBuilder(
            db_session, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )
        results = builder.query_with_filter(FPLSnapshot)
        # The future snapshot should NOT be in the results
        for r in results:
            assert _ensure_aware(r.available_at) <= cutoff
            assert _ensure_aware(r.ingested_at) <= cutoff

    def test_past_data_included_in_query(self, db_session, populated_db) -> None:
        """Test that data with available_at <= cutoff is included in queries."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        builder = TemporalQueryBuilder(
            db_session, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )
        results = builder.query_with_filter(FPLSnapshot)
        # All results should have available_at <= cutoff
        for r in results:
            assert _ensure_aware(r.available_at) <= cutoff
            assert _ensure_aware(r.ingested_at) <= cutoff

    def test_public_policy_ignores_ingestion_time(self, db_session, populated_db) -> None:
        """Test that PUBLIC_AVAILABILITY policy ignores ingested_at."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        # Add a snapshot that was available before cutoff but ingested after
        snapshot = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),  # After cutoff
            price=10.0,
        )
        db_session.add(snapshot)
        db_session.commit()

        # Under PUBLIC_AVAILABILITY, this should be available
        assert is_record_available(snapshot, cutoff, InformationAccessPolicy.PUBLIC_AVAILABILITY)
        # Under STRICT_REPRODUCIBILITY, this should NOT be available
        assert not is_record_available(
            snapshot, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )

    def test_system_policy_ignores_available_at(self, db_session, populated_db) -> None:
        """Test that SYSTEM_AVAILABILITY policy ignores available_at."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        # Add a snapshot that was ingested before cutoff but available after
        snapshot = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),  # After cutoff
            ingested_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),  # Before cutoff
            price=10.0,
        )
        db_session.add(snapshot)
        db_session.commit()

        # Under SYSTEM_AVAILABILITY, this should be available
        assert is_record_available(snapshot, cutoff, InformationAccessPolicy.SYSTEM_AVAILABILITY)
        # Under STRICT_REPRODUCIBILITY, this should NOT be available
        assert not is_record_available(
            snapshot, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )


class TestCalculatorNoLookAhead:
    """Tests that feature calculators respect the no-look-ahead rule."""

    def test_player_form_excludes_future_data(self, db_session, populated_db) -> None:
        """Test that PlayerFormCalculator doesn't use future data."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        # Add a future performance record

        gw = db_session.scalar(select(Gameweek).where(Gameweek.provider_event_id == 3))
        future_perf = PlayerGameweekPerformance(
            player_id=1,
            gameweek_id=gw.id if gw else 3,
            season_id=1,
            team_id=1,
            total_points=100,  # Very high to detect leakage
            minutes=90,
            available_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
        )
        db_session.add(future_perf)
        db_session.commit()

        calc = PlayerFormCalculator()
        result = calc.compute(1, cutoff, {"db": db_session})

        # The future performance should NOT be included
        value = result["value"]
        # rolling_points_3gw should not include the 100-point future record
        assert value.get("rolling_points_3gw", 0) < 100

    def test_market_features_excludes_future_snapshots(self, db_session, populated_db) -> None:
        """Test that MarketFeaturesCalculator doesn't use future snapshots."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        # Add a future snapshot with very high price
        future_snapshot = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            price=50.0,  # Very high to detect leakage
            total_points=100,
        )
        db_session.add(future_snapshot)
        db_session.commit()

        calc = MarketFeaturesCalculator()
        result = calc.compute(1, cutoff, {"db": db_session})

        value = result["value"]
        # The future price should NOT be used
        assert value.get("price", 0) < 50.0

    def test_calculator_respects_strict_policy(self, db_session, populated_db) -> None:
        """Test that calculators respect STRICT_REPRODUCIBILITY policy."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        # Add a snapshot available before cutoff but ingested after
        snapshot = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),  # After cutoff
            price=15.0,
            total_points=30,
        )
        db_session.add(snapshot)
        db_session.commit()

        calc = MarketFeaturesCalculator()
        result = calc.compute(
            1,
            cutoff,
            {"db": db_session, "policy": InformationAccessPolicy.STRICT_REPRODUCIBILITY},
        )

        # Under strict policy, this snapshot should NOT be used
        # because ingested_at > cutoff
        value = result["value"]
        # The price should come from earlier snapshots, not this one
        assert value.get("price", 0) != 15.0

    def test_calculator_uses_public_policy(self, db_session, populated_db) -> None:
        """Test that calculators can use PUBLIC_AVAILABILITY policy."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        # Add a snapshot available before cutoff but ingested after
        snapshot = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),  # After cutoff
            price=15.0,
            total_points=30,
        )
        db_session.add(snapshot)
        db_session.commit()

        calc = MarketFeaturesCalculator()
        result = calc.compute(
            1,
            cutoff,
            {"db": db_session, "policy": InformationAccessPolicy.PUBLIC_AVAILABILITY},
        )

        # Under public policy, this snapshot SHOULD be used
        # because available_at <= cutoff
        value = result["value"]
        assert value.get("price") == 15.0


class TestCacheNoLeakage:
    """Tests that the feature cache doesn't cause leakage."""

    def test_cache_key_includes_cutoff(self) -> None:
        """Test that cache keys include the cutoff time."""
        from fpl_intelligence.features.cache import FeatureCache

        cache = FeatureCache()
        cutoff1 = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        cutoff2 = datetime(2025, 8, 16, 12, 0, 0, tzinfo=UTC)

        cache.set("test_feature", "1.0.0", 1, cutoff1, {"value": 1.0})
        result = cache.get("test_feature", "1.0.0", 1, cutoff2)
        assert result is None  # Different cutoff = cache miss

    def test_cache_key_includes_version(self) -> None:
        """Test that cache keys include the feature version."""
        from fpl_intelligence.features.cache import FeatureCache

        cache = FeatureCache()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        cache.set("test_feature", "1.0.0", 1, cutoff, {"value": 1.0})
        result = cache.get("test_feature", "2.0.0", 1, cutoff)
        assert result is None  # Different version = cache miss
