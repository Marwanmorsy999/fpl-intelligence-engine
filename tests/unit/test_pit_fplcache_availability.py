from __future__ import annotations

import json
import lzma
from datetime import UTC, datetime
from pathlib import Path

from fpl_intelligence.availability.historical.pit_fplcache import (
    PointInTimeFPLCacheAvailabilityProvider,
    SnapshotRef,
)


def _write_snapshot(root: Path, captured_at: datetime, elements: list[dict]) -> Path:
    day = root / str(captured_at.year) / str(captured_at.month) / str(captured_at.day)
    day.mkdir(parents=True)
    path = day / f"{captured_at:%H%M}.json.xz"
    with lzma.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"elements": elements}, fh)
    return path


def test_latest_before_returns_immutable_snapshot_timestamp(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    first = datetime(2025, 8, 15, 6, 0, tzinfo=UTC)
    second = datetime(2025, 8, 15, 12, 0, tzinfo=UTC)
    _write_snapshot(root, first, [])
    _write_snapshot(root, second, [])

    provider = PointInTimeFPLCacheAvailabilityProvider(root)
    ref = provider.latest_before(datetime(2025, 8, 15, 11, 0, tzinfo=UTC))

    assert ref is not None
    assert ref.captured_at == first


def test_snapshot_emits_flagged_players_with_snapshot_availability_time(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    captured = datetime(2025, 8, 15, 6, 0, tzinfo=UTC)
    path = _write_snapshot(
        root,
        captured,
        [
            {
                "id": 10,
                "team": 1,
                "status": "d",
                "chance_of_playing_this_round": 75,
                "chance_of_playing_next_round": 100,
                "news": "Knock",
            },
            {
                "id": 11,
                "team": 1,
                "status": "a",
                "chance_of_playing_this_round": 100,
                "news": "",
            },
        ],
    )

    provider = PointInTimeFPLCacheAvailabilityProvider(root)
    events = provider.events_from_snapshot(
        "2025-26", SnapshotRef(captured, path), gameweek=1
    )

    assert len(events) == 1
    event = events[0]
    assert event["player_id"] == "10"
    assert event["status"] == "doubtful"
    assert event["gameweek"] == 1
    assert event["timestamps"].published_at == captured
    assert event["timestamps"].available_at == captured
    assert event["timestamps"].ingested_at is not None
    assert event["provider"] == "fplcache_pit"
    assert event["snapshot_captured_at"] == captured


def test_available_player_with_sub100_chance_is_questionable(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    captured = datetime(2025, 8, 15, 12, 0, tzinfo=UTC)
    path = _write_snapshot(
        root,
        captured,
        [
            {
                "id": 42,
                "team": 2,
                "status": "a",
                "chance_of_playing_this_round": 50,
                "chance_of_playing_next_round": 75,
                "news": "Managing a knock",
            }
        ],
    )

    provider = PointInTimeFPLCacheAvailabilityProvider(root)
    event = provider.events_from_snapshot("2025-26", SnapshotRef(captured, path))[0]

    assert event["status"] == "questionable"
    assert event["chance_of_playing_this_round"] == 50
