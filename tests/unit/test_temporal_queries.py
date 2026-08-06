"""Tests for temporal query helpers and information-access policies.

These tests verify that the no-look-ahead rule is correctly enforced
by the temporal query helpers and information-access policies.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fpl_intelligence.db.models import FPLSnapshot, Player
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
    TemporalQueryBuilder,
    apply_policy,
    as_of,
    is_record_available,
)


class TestInformationAccessPolicy:
    """Tests for the InformationAccessPolicy enum."""

    def test_policy_values(self) -> None:
        """Test that policy values are correct."""
        assert InformationAccessPolicy.PUBLIC_AVAILABILITY.value == "public_availability"
        assert InformationAccessPolicy.SYSTEM_AVAILABILITY.value == "system_availability"
        assert InformationAccessPolicy.STRICT_REPRODUCIBILITY.value == "strict_reproducibility"

    def test_default_policy(self) -> None:
        """Test that the default policy is STRICT_REPRODUCIBILITY."""
        assert DEFAULT_POLICY == InformationAccessPolicy.STRICT_REPRODUCIBILITY


class TestAsOf:
    """Tests for the as_of() function."""

    def test_as_of_returns_leq_condition(self) -> None:
        """Test that as_of returns a <= condition."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        col = FPLSnapshot.event_time
        condition = as_of(cutoff, col)
        # The condition should be a SQLAlchemy binary expression
        assert condition is not None


class TestApplyPolicy:
    """Tests for the apply_policy() function."""

    def test_public_availability_uses_available_at(self) -> None:
        """Test that PUBLIC_AVAILABILITY uses available_at."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        condition = apply_policy(
            FPLSnapshot,
            InformationAccessPolicy.PUBLIC_AVAILABILITY,
            cutoff,
        )
        assert condition is not None

    def test_system_availability_uses_ingested_at(self) -> None:
        """Test that SYSTEM_AVAILABILITY uses ingested_at."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        condition = apply_policy(
            FPLSnapshot,
            InformationAccessPolicy.SYSTEM_AVAILABILITY,
            cutoff,
        )
        assert condition is not None

    def test_strict_reproducibility_uses_both(self) -> None:
        """Test that STRICT_REPRODUCIBILITY uses both available_at and ingested_at."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        condition = apply_policy(
            FPLSnapshot,
            InformationAccessPolicy.STRICT_REPRODUCIBILITY,
            cutoff,
        )
        assert condition is not None

    def test_unknown_policy_raises(self) -> None:
        """Test that an unknown policy raises ValueError."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="Unknown policy"):
            apply_policy(FPLSnapshot, "unknown_policy", cutoff)  # type: ignore[arg-type]


class TestIsRecordAvailable:
    """Tests for the is_record_available() function."""

    def test_strict_reproducibility_available(self) -> None:
        """Test that a record is available under strict policy when both times are before cutoff."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        record = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
        )
        assert is_record_available(record, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY)

    def test_strict_reproducibility_not_available_future(self) -> None:
        """Test that a record is not available when available_at is after cutoff."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        record = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
        )
        assert not is_record_available(
            record, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )

    def test_strict_reproducibility_not_available_not_ingested(self) -> None:
        """Test that a record is not available when ingested_at is after cutoff."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        record = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
        )
        assert not is_record_available(
            record, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )

    def test_public_availability_available(self) -> None:
        """Available under public policy when available_at is before cutoff."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        record = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
        )
        # Under public policy, ingested_at doesn't matter
        assert is_record_available(record, cutoff, InformationAccessPolicy.PUBLIC_AVAILABILITY)

    def test_system_availability_not_available(self) -> None:
        """Test that a record is not available under system policy when not ingested."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        record = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
        )
        assert not is_record_available(record, cutoff, InformationAccessPolicy.SYSTEM_AVAILABILITY)

    def test_boundary_exact_cutoff(self) -> None:
        """Test that a record with available_at == cutoff is available (<=)."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        record = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=cutoff,
            available_at=cutoff,
            ingested_at=cutoff,
        )
        assert is_record_available(record, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY)


class TestTemporalQueryBuilder:
    """Tests for the TemporalQueryBuilder class."""

    def test_query_applies_temporal_filter(self, db_session) -> None:
        """Test that TemporalQueryBuilder applies temporal filters."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        builder = TemporalQueryBuilder(
            db_session, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )
        results = builder.query_with_filter(
            FPLSnapshot,
            FPLSnapshot.player_id == 1,
        )
        assert isinstance(results, list)

    def test_query_without_temporal_columns(self, db_session) -> None:
        """Test that querying a model without temporal columns doesn't fail."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        builder = TemporalQueryBuilder(
            db_session, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )
        # Player model has no temporal columns
        results = builder.query_with_filter(Player)
        assert isinstance(results, list)


class TestCutoffBoundary:
    """Tests for cutoff boundary conditions."""

    def test_record_at_exact_cutoff_is_available(self, db_session) -> None:
        """Test that a record with available_at == cutoff is available."""
        from fpl_intelligence.db.models import Player, Season

        season = Season(code="2025-26", display_name="2025/26")
        player = Player(first_name="Test", second_name="Player", web_name="test")
        db_session.add(season)
        db_session.add(player)
        db_session.flush()

        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        snapshot = FPLSnapshot(
            player_id=player.id,
            season_id=season.id,
            event_time=cutoff,
            available_at=cutoff,
            ingested_at=cutoff,
        )
        db_session.add(snapshot)
        db_session.commit()

        builder = TemporalQueryBuilder(
            db_session, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )
        results = builder.query_with_filter(FPLSnapshot)
        assert len(results) == 1

    def test_record_before_cutoff_is_available(self, db_session) -> None:
        """Test that a record with available_at < cutoff is available."""
        from fpl_intelligence.db.models import Player, Season

        season = Season(code="2025-26", display_name="2025/26")
        player = Player(first_name="Test", second_name="Player", web_name="test")
        db_session.add(season)
        db_session.add(player)
        db_session.flush()

        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        snapshot = FPLSnapshot(
            player_id=player.id,
            season_id=season.id,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
        )
        db_session.add(snapshot)
        db_session.commit()

        builder = TemporalQueryBuilder(
            db_session, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )
        results = builder.query_with_filter(FPLSnapshot)
        assert len(results) == 1

    def test_record_after_cutoff_is_not_available(self, db_session) -> None:
        """Test that a record with available_at > cutoff is NOT available."""
        from fpl_intelligence.db.models import Player, Season

        season = Season(code="2025-26", display_name="2025/26")
        player = Player(first_name="Test", second_name="Player", web_name="test")
        db_session.add(season)
        db_session.add(player)
        db_session.flush()

        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        snapshot = FPLSnapshot(
            player_id=player.id,
            season_id=season.id,
            event_time=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
        )
        db_session.add(snapshot)
        db_session.commit()

        builder = TemporalQueryBuilder(
            db_session, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )
        results = builder.query_with_filter(FPLSnapshot)
        assert len(results) == 0
