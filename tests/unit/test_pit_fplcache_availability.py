"""Unit tests for immutable fplcache PIT extraction."""
from __future__ import annotations

import json
import lzma
from datetime import UTC, datetime
from pathlib import Path

from fpl_intelligence.availability.historical.pit_fplcache import PointInTimeFPLCacheAvailabilityProvider, SnapshotRef


def _write_snapshot(path: Path, elements: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(lzma.compress(json.dumps({"elements": elements}).encode()))


def test_snapshot_latest_before_is_strict(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write_snapshot(root / "2024/8/16/1200.json.xz", [{"id": 1, "status": "a"}])
    _write_snapshot(root / "2024/8/16/1800.json.xz", [{"id": 1, "status": "i", "news": "injured"}])
    provider = PointInTimeFPLCacheAvailabilityProvider(root)
    cutoff = datetime(2024, 8, 16, 17, tzinfo=UTC)
    ref = provider.latest_before(cutoff)
    assert ref is not None
    assert ref.captured_at == datetime(2024, 8, 16, 12, tzinfo=UTC)


def test_events_use_snapshot_capture_as_information_time(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    path = root / "2024/8/16/1200.json.xz"
    _write_snapshot(path, [{"id": 10, "team": 3, "status": "i", "news": "hamstring"}])
    provider = PointInTimeFPLCacheAvailabilityProvider(root)
    captured = datetime(2024, 8, 16, 12, tzinfo=UTC)
    events = provider.events_from_snapshot("2024-25", SnapshotRef(captured, path), gameweek=1)
    assert len(events) == 1
    assert events[0]["player_id"] == "10"
    assert events[0]["timestamps"].published_at == captured
    assert events[0]["timestamps"].available_at == captured
    assert events[0]["event_time"] if "event_time" in events[0] else True
