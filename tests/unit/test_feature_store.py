"""Tests for the feature store: models, cache, registry, and calculators."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fpl_intelligence.features.cache import FeatureCache
from fpl_intelligence.features.calculators.availability import PlayerAvailabilityCalculator
from fpl_intelligence.features.calculators.market_features import MarketFeaturesCalculator
from fpl_intelligence.features.calculators.player_form import PlayerFormCalculator
from fpl_intelligence.features.models import FeatureDefinition, FeatureLineage, FeatureSnapshot
from fpl_intelligence.features.registry import FeatureRegistry


class TestFeatureCache:
    """Tests for the FeatureCache class."""

    def test_cache_miss_returns_none(self) -> None:
        """Test that a cache miss returns None."""
        cache = FeatureCache()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        result = cache.get("test_feature", "1.0.0", 1, cutoff)
        assert result is None

    def test_cache_hit_returns_value(self) -> None:
        """Test that a cache hit returns the stored value."""
        cache = FeatureCache()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        value = {"test": 1.0, "is_missing": False}
        cache.set("test_feature", "1.0.0", 1, cutoff, value)
        result = cache.get("test_feature", "1.0.0", 1, cutoff)
        assert result is not None
        assert result["test"] == 1.0

    def test_cache_different_cutoff_is_miss(self) -> None:
        """Test that a different cutoff results in a cache miss."""
        cache = FeatureCache()
        cutoff1 = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        cutoff2 = datetime(2025, 8, 16, 12, 0, 0, tzinfo=UTC)
        value = {"test": 1.0}
        cache.set("test_feature", "1.0.0", 1, cutoff1, value)
        result = cache.get("test_feature", "1.0.0", 1, cutoff2)
        assert result is None

    def test_cache_different_version_is_miss(self) -> None:
        """Test that a different version results in a cache miss."""
        cache = FeatureCache()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        value = {"test": 1.0}
        cache.set("test_feature", "1.0.0", 1, cutoff, value)
        result = cache.get("test_feature", "2.0.0", 1, cutoff)
        assert result is None

    def test_cache_clear(self) -> None:
        """Test that clear() empties the cache."""
        cache = FeatureCache()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        cache.set("test_feature", "1.0.0", 1, cutoff, {"test": 1.0})
        cache.clear()
        result = cache.get("test_feature", "1.0.0", 1, cutoff)
        assert result is None

    def test_cache_stats(self) -> None:
        """Test that cache stats are tracked."""
        cache = FeatureCache()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        cache.get("test_feature", "1.0.0", 1, cutoff)  # miss
        cache.set("test_feature", "1.0.0", 1, cutoff, {"test": 1.0})
        cache.get("test_feature", "1.0.0", 1, cutoff)  # hit
        stats = cache.stats
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["size"] == 1


class TestFeatureRegistry:
    """Tests for the FeatureRegistry class."""

    def test_register_and_get(self, db_session) -> None:
        """Test registering and retrieving a calculator."""
        registry = FeatureRegistry(db_session)
        calc = PlayerFormCalculator()
        registry.register(calc)
        assert registry.get("player_form") is calc

    def test_get_unregistered_raises(self, db_session) -> None:
        """Test that getting an unregistered feature raises KeyError."""
        registry = FeatureRegistry(db_session)
        with pytest.raises(KeyError):
            registry.get("nonexistent")

    def test_list_features(self, db_session) -> None:
        """Test listing registered features."""
        registry = FeatureRegistry(db_session)
        registry.register(PlayerFormCalculator())
        registry.register(MarketFeaturesCalculator())
        features = registry.list_features()
        assert "player_form" in features
        assert "market_features" in features

    def test_compute_single_feature(self, db_session, populated_db) -> None:
        """Test computing a single feature."""
        registry = FeatureRegistry(db_session)
        registry.register(PlayerFormCalculator())

        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        result = registry.compute("player_form", 1, cutoff)
        assert "value" in result
        assert "is_missing" in result
        assert "completeness_score" in result

    def test_compute_all_features(self, db_session, populated_db) -> None:
        """Test computing all features for an entity."""
        registry = FeatureRegistry(db_session)
        registry.register(PlayerFormCalculator())
        registry.register(MarketFeaturesCalculator())

        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        results = registry.compute_all(1, cutoff)
        assert "player_form" in results
        assert "market_features" in results

    def test_compute_batch(self, db_session, populated_db) -> None:
        """Test computing a feature for multiple entities."""
        registry = FeatureRegistry(db_session)
        registry.register(PlayerFormCalculator())

        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        results = registry.compute_batch("player_form", [1, 2, 3], cutoff)
        assert len(results) == 3
        assert 1 in results
        assert 2 in results
        assert 3 in results

    def test_compute_features_interface(self, db_session, populated_db) -> None:
        """Test the compute_features interface used by BacktestEngine."""
        from fpl_intelligence.backtesting.cutoff import DecisionCutoff

        registry = FeatureRegistry(db_session)
        registry.register(PlayerFormCalculator())
        registry.register(MarketFeaturesCalculator())

        cutoff = DecisionCutoff(
            cutoff_time=datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC),
            gameweek=1,
            season="2025-26",
        )
        features = registry.compute_features(db_session, cutoff)
        assert isinstance(features, dict)


class TestPlayerFormCalculator:
    """Tests for the PlayerFormCalculator."""

    def test_compute_with_data(self, db_session, populated_db) -> None:
        """Test computing player form with existing data."""
        calc = PlayerFormCalculator()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        result = calc.compute(1, cutoff, {"db": db_session})
        assert result["is_missing"] is False
        assert result["completeness_score"] > 0
        value = result["value"]
        assert "rolling_points_3gw" in value
        assert "rolling_points_5gw" in value
        assert "rolling_points_8gw" in value
        assert "form_weighted_points" in value
        assert "consistency_score" in value

    def test_compute_without_data(self, db_session) -> None:
        """Test computing player form with no data."""
        calc = PlayerFormCalculator()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        result = calc.compute(999, cutoff, {"db": db_session})
        assert result["is_missing"] is True
        assert result["completeness_score"] == 0.0

    def test_get_all_entity_ids(self, db_session, populated_db) -> None:
        """Test getting all entity IDs."""
        calc = PlayerFormCalculator()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        ids = calc.get_all_entity_ids(db_session, cutoff, {"db": db_session})
        assert len(ids) > 0


class TestMarketFeaturesCalculator:
    """Tests for the MarketFeaturesCalculator."""

    def test_compute_with_data(self, db_session, populated_db) -> None:
        """Test computing market features with existing data."""
        calc = MarketFeaturesCalculator()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        result = calc.compute(1, cutoff, {"db": db_session})
        assert result["is_missing"] is False
        value = result["value"]
        assert "price" in value
        assert "ownership" in value
        assert "form" in value

    def test_compute_without_data(self, db_session) -> None:
        """Test computing market features with no data."""
        calc = MarketFeaturesCalculator()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        result = calc.compute(999, cutoff, {"db": db_session})
        assert result["is_missing"] is True


class TestPlayerAvailabilityCalculator:
    """Tests for the PlayerAvailabilityCalculator."""

    def test_compute_returns_null_features(self, db_session, populated_db) -> None:
        """Test that availability calculator returns null features with is_missing=True."""
        calc = PlayerAvailabilityCalculator()
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        result = calc.compute(1, cutoff, {"db": db_session})
        assert result["is_missing"] is True
        assert result["completeness_score"] == 0.0
        value = result["value"]
        assert value["injury_status"] is None
        assert value["suspension_status"] is None
        assert value["availability_status"] is None
        assert value["has_historical_injury_data"] is False


class TestFeatureModels:
    """Tests for the feature store database models."""

    def test_feature_definition_creation(self, db_session) -> None:
        """Test creating a FeatureDefinition."""
        definition = FeatureDefinition(
            feature_name="test_feature",
            description="Test feature",
            data_type="json",
            entity_type="player",
            version="1.0.0",
            calculation_method="TestCalculator",
        )
        db_session.add(definition)
        db_session.commit()
        assert definition.id is not None

    def test_feature_snapshot_creation(self, db_session) -> None:
        """Test creating a FeatureSnapshot."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        snapshot = FeatureSnapshot(
            entity_id=1,
            feature_name="test_feature",
            feature_version="1.0.0",
            cutoff_time=cutoff,
            value={"test": 1.0},
            is_missing=False,
            completeness_score=1.0,
            source_count=5,
        )
        db_session.add(snapshot)
        db_session.commit()
        assert snapshot.id is not None

    def test_feature_lineage_creation(self, db_session) -> None:
        """Test creating a FeatureLineage record."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        lineage = FeatureLineage(
            feature_name="test_feature",
            feature_version="1.0.0",
            entity_id=1,
            source_table="fpl_snapshots",
            source_record_ids=[1, 2, 3],
            calculation_version="1.0.0",
            cutoff_time=cutoff,
        )
        db_session.add(lineage)
        db_session.commit()
        assert lineage.id is not None
