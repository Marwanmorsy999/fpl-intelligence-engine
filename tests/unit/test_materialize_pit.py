from __future__ import annotations

import json
import lzma
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fpl_intelligence.availability.historical.materialize_pit import (
    DeadlineCutoff,
    collect_events,
    local_snapshot_path,
    materialize_cutoffs,
)
from fpl_intelligence.availability.historical.normalizer import normalize_event
from fpl_intelligence.availability.historical.pit_fplcache import (
    PointInTimeFPLCacheAvailabilityProvider,
    SnapshotRef,
)
from fpl_intelligence.availability.historical.temporal import AvailabilityTimestamps


def _write_snapshot(root: Path, captured_at: datetime, elements: list[dict]) -> Path:
    path = local_snapshot_path(root, captured_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    with lzma.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"elements": elements}, fh)
    return path


def test_local_snapshot_path_layout() -> None:
    captured = datetime(2025, 8, 15, 6, 0, tzinfo=UTC)
    path = local_snapshot_path(Path("/tmp/cache"), captured)
    assert path.as_posix().endswith("2025/8/15/0600.json.xz")


def test_materialize_reuses_local_snapshot_without_network(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    captured = datetime(2025, 8, 15, 6, 0, tzinfo=UTC)
    _write_snapshot(
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

    cutoff = DeadlineCutoff(
        season_code="2025-26",
        gameweek=1,
        cutoff=datetime(2025, 8, 15, 11, 0, tzinfo=UTC),
    )

    with patch(
        "fpl_intelligence.availability.historical.materialize_pit.latest_remote_before",
        return_value=(
            captured,
            "https://raw.githubusercontent.com/Randdalf/fplcache/main/cache/2025/8/15/0600.json.xz",
        ),
    ):
        report = materialize_cutoffs(root, [cutoff])

    assert report.missing == 0
    assert report.reused == 1
    assert report.downloaded == 0
    assert report.event_count == 1
    assert report.snapshots[0].flagged_count == 1
    events = collect_events(report)
    assert events[0]["player_id"] == "10"
    assert events[0]["timestamps"].available_at == captured
    assert events[0]["gameweek"] == 1


def test_normalizer_preserves_provider_status_when_valid() -> None:
    captured = datetime(2025, 8, 15, 6, 0, tzinfo=UTC)
    raw = {
        "provider": "fplcache_pit",
        "provider_event_id": f"{captured.isoformat()}:2025-26:10",
        "player_id": "10",
        "team_id": "1",
        "event_type": "fitness",
        "status": "questionable",
        "description": "chance_of_playing_this_round=50",
        "timestamps": AvailabilityTimestamps(
            event_time=None,
            published_at=captured,
            available_at=captured,
            ingested_at=captured,
        ),
    }
    norm = normalize_event(raw)
    assert norm["status"] == "questionable"
    assert norm["event_type"] == "fitness"


def test_provider_events_normalize_to_strict_safe_timestamps(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    captured = datetime(2024, 8, 16, 12, 0, tzinfo=UTC)
    path = _write_snapshot(
        root,
        captured,
        [
            {
                "id": 99,
                "team": 3,
                "status": "i",
                "chance_of_playing_this_round": 0,
                "news": "Hamstring injury",
            }
        ],
    )
    provider = PointInTimeFPLCacheAvailabilityProvider(root)
    events = provider.events_from_snapshot("2024-25", SnapshotRef(captured, path), gameweek=1)
    assert len(events) == 1
    norm = normalize_event(events[0])
    assert norm["status"] in {"out", "doubtful", "questionable", "suspended", "unknown"}
    temporal = norm["temporal"]
    assert temporal["published_at"] is not None
    assert temporal["available_at"] is not None
