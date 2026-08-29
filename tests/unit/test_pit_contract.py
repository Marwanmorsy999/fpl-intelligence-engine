from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_intelligence.availability.historical.pit_contract import validate_pit_events
from fpl_intelligence.availability.historical.temporal import AvailabilityTimestamps


def _event(at: datetime) -> dict[str, object]:
    return {
        "provider": "fplcache_pit",
        "season_code": "2025-26",
        "player_id": "1",
        "provider_event_id": f"{at.isoformat()}:2025-26:1",
        "gameweek": 2,
        "snapshot_captured_at": at,
        "timestamps": AvailabilityTimestamps(
            event_time=None,
            published_at=at,
            available_at=at,
            ingested_at=at + timedelta(minutes=1),
        ),
    }


def test_valid_event_passes_contract() -> None:
    at = datetime(2025, 8, 20, 10, tzinfo=UTC)
    result = validate_pit_events(
        [_event(at)],
        cutoffs={("2025-26", 2): at + timedelta(hours=1)},
    )
    assert result == {"checked": 1, "eligible_before_cutoff": 1, "unique": 1}


def test_duplicate_event_is_rejected() -> None:
    at = datetime(2025, 8, 20, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="duplicate PIT provider event"):
        validate_pit_events(
            [_event(at), _event(at)],
            cutoffs={("2025-26", 2): at + timedelta(hours=1)},
        )


def test_post_deadline_event_is_rejected() -> None:
    at = datetime(2025, 8, 20, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="post-deadline"):
        validate_pit_events(
            [_event(at)],
            cutoffs={("2025-26", 2): at - timedelta(minutes=1)},
        )


def test_snapshot_and_available_at_must_match() -> None:
    at = datetime(2025, 8, 20, 10, tzinfo=UTC)
    bad = _event(at)
    bad["timestamps"] = AvailabilityTimestamps(
        event_time=None,
        published_at=at,
        available_at=at + timedelta(minutes=1),
        ingested_at=at + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="available_at must equal snapshot capture time"):
        validate_pit_events(
            [bad],
            cutoffs={("2025-26", 2): at + timedelta(hours=1)},
        )
