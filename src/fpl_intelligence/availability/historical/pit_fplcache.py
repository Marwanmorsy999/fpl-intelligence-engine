"""Point-in-time historical availability from immutable Randdalf/fplcache snapshots.

Strict PIT rule: the snapshot capture time is the information-availability time;
football event time is never substituted for it. This module is read-only with
respect to the database and only consumes locally materialized snapshots.
"""
from __future__ import annotations

import json
import lzma
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from fpl_intelligence.availability.historical.event_types import parse_event_type
from fpl_intelligence.availability.historical.providers import HistoricalAvailabilityProvider
from fpl_intelligence.availability.historical.temporal import AvailabilityTimestamps

FPLCACHE_REPO = "Randdalf/fplcache"
FPLCACHE_RAW_BASE = "https://raw.githubusercontent.com/Randdalf/fplcache/main/cache"


@dataclass(frozen=True)
class SnapshotRef:
    """Immutable local snapshot plus its capture time."""
    captured_at: datetime
    path: Path


class PointInTimeFPLCacheAvailabilityProvider(HistoricalAvailabilityProvider):
    """Availability provider backed by timestamped fplcache bootstrap snapshots."""

    environment = "real"

    def __init__(self, snapshot_root: Path, *, seasons: Iterable[str] | None = None) -> None:
        self.snapshot_root = snapshot_root
        self._seasons = list(seasons or ["2022-23", "2023-24", "2024-25", "2025-26"])
        self._cache: dict[str, list[dict[str, Any]]] = {}

    @property
    def provider_name(self) -> str:
        return "fplcache_pit"

    @property
    def source_name(self) -> str:
        return f"{FPLCACHE_REPO} point-in-time FPL bootstrap"

    @property
    def seasons_covered(self) -> list[str]:
        return list(self._seasons)

    def snapshots_for_window(self, start: datetime, end: datetime) -> list[SnapshotRef]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("snapshot window must be timezone-aware")
        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        out: list[SnapshotRef] = []
        cursor = start_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor <= end_utc:
            day_dir = self.snapshot_root / str(cursor.year) / str(cursor.month) / str(cursor.day)
            if day_dir.exists():
                for path in sorted(day_dir.glob("*.json.xz")):
                    stem = path.name.removesuffix(".json.xz")
                    if len(stem) != 4 or not stem.isdigit():
                        continue
                    try:
                        captured = cursor.replace(hour=int(stem[:2]), minute=int(stem[2:]))
                    except ValueError:
                        continue
                    if start_utc <= captured <= end_utc:
                        out.append(SnapshotRef(captured, path))
            cursor += timedelta(days=1)
        return sorted({(r.captured_at, r.path): r for r in out}.values(), key=lambda r: r.captured_at)

    def latest_before(self, cutoff: datetime, *, search_days: int = 3) -> SnapshotRef | None:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        refs = self.snapshots_for_window(
            cutoff.astimezone(UTC) - timedelta(days=search_days), cutoff.astimezone(UTC)
        )
        return refs[-1] if refs else None

    def load_snapshot(self, ref: SnapshotRef) -> dict[str, Any]:
        with lzma.open(ref.path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
            raise ValueError(f"invalid fplcache snapshot: {ref.path}")
        return payload

    def events_from_snapshot(
        self, season: str, ref: SnapshotRef, *, gameweek: int | None = None
    ) -> list[dict[str, Any]]:
        payload = self.load_snapshot(ref)
        captured = ref.captured_at.astimezone(UTC)
        events: list[dict[str, Any]] = []
        for player in payload["elements"]:
            player_id = str(player.get("id") or "").strip()
            if not player_id:
                continue
            status_code = str(player.get("status") or "a").strip().lower()
            chance_this = _int_or_none(player.get("chance_of_playing_this_round"))
            chance_next = _int_or_none(player.get("chance_of_playing_next_round"))
            news = str(player.get("news") or "").strip()
            # The provider emits only non-default states. This keeps imports event-like
            # while preserving complete snapshot provenance for each emitted row.
            if _is_default_available(status_code, chance_this, news):
                continue
            status = _map_status(status_code, chance_this)
            event_type = parse_event_type([news, status_code]) if news else _event_type_from_status(status_code)
            provider_event_id = f"{captured.isoformat()}:{season}:{player_id}"
            events.append(
                {
                    "provider_event_id": provider_event_id,
                    "provider": self.provider_name,
                    "environment": self.environment,
                    "season_code": season,
                    "player_id": player_id,
                    "team_id": str(player.get("team") or "").strip() or None,
                    "gameweek": gameweek,
                    "event_type": event_type,
                    "status": status,
                    "description": news or _chance_description(chance_this),
                    "chance_of_playing_this_round": chance_this,
                    "chance_of_playing_next_round": chance_next,
                    "news_added": captured,
                    "timestamps": AvailabilityTimestamps(
                        event_time=None,
                        published_at=captured,
                        available_at=captured,
                        ingested_at=datetime.now(UTC),
                    ),
                    "source_name": self.source_name,
                    "reliability": "official",
                    "snapshot_captured_at": captured,
                    "snapshot_path": ref.path.as_posix(),
                    "snapshot_source_url": f"{FPLCACHE_RAW_BASE}/{ref.path.relative_to(self.snapshot_root).as_posix()}",
                }
            )
        return events

    def fetch_events(self, season: str) -> list[dict[str, Any]]:
        if season in self._cache:
            return list(self._cache[season])
        start, end = _season_window(season)
        events: list[dict[str, Any]] = []
        for ref in self.snapshots_for_window(start, end):
            events.extend(self.events_from_snapshot(season, ref))
        self._cache[season] = events
        return list(events)


def _season_window(season: str) -> tuple[datetime, datetime]:
    windows = {
        "2022-23": (datetime(2022, 8, 1, tzinfo=UTC), datetime(2023, 6, 1, tzinfo=UTC)),
        "2023-24": (datetime(2023, 8, 1, tzinfo=UTC), datetime(2024, 6, 1, tzinfo=UTC)),
        "2024-25": (datetime(2024, 8, 1, tzinfo=UTC), datetime(2025, 6, 1, tzinfo=UTC)),
        "2025-26": (datetime(2025, 8, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)),
    }
    try:
        return windows[season]
    except KeyError as exc:
        raise ValueError(f"unsupported season {season}") from exc


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _is_default_available(status_code: str, chance_this: int | None, news: str) -> bool:
    return status_code == "a" and chance_this in (None, 100) and not news


def _map_status(status_code: str, chance_this: int | None) -> str:
    status = {
        "a": "available",
        "d": "doubtful",
        "i": "out",
        "s": "suspended",
        "u": "out",
        "n": "unknown",
    }.get(status_code, "unknown")
    if status == "available" and chance_this is not None and chance_this < 100:
        return "questionable"
    return status


def _event_type_from_status(status_code: str) -> str:
    if status_code == "s":
        return "suspension"
    if status_code in {"i", "d", "u"}:
        return "injury"
    return "fitness"


def _chance_description(chance_this: int | None) -> str | None:
    return None if chance_this in (None, 100) else f"chance_of_playing_this_round={chance_this}"
