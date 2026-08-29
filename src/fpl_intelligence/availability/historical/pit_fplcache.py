"""Point-in-time historical availability from Randdalf/fplcache.

The repository stores immutable compressed snapshots of FPL ``bootstrap-static``
roughly every six hours. Unlike the season-end ``players_raw.csv`` mirror, a
snapshot timestamp gives us an explicit information-availability boundary for
status/news/chance-of-playing fields.

This module intentionally does not mutate the database or depend on a local
fplcache checkout. It can read snapshots from disk, and the ingestion script can
materialize a small set of deadline-adjacent snapshots from the public GitHub
repository.
"""

from __future__ import annotations

import json
import lzma
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from fpl_intelligence.availability.historical.event_types import parse_event_type
from fpl_intelligence.availability.historical.temporal import AvailabilityTimestamps
from fpl_intelligence.availability.historical.providers import HistoricalAvailabilityProvider


FPLCACHE_REPO = "Randdalf/fplcache"
FPLCACHE_RAW_BASE = "https://raw.githubusercontent.com/Randdalf/fplcache/main/cache"


@dataclass(frozen=True)
class SnapshotRef:
    """One immutable fplcache snapshot and its observation time."""

    captured_at: datetime
    path: Path


class PointInTimeFPLCacheAvailabilityProvider(HistoricalAvailabilityProvider):
    """REAL point-in-time availability provider backed by fplcache snapshots.

    The source is the public ``Randdalf/fplcache`` repository, which documents
    that FPL ``bootstrap-static`` snapshots are cached four times per day in
    ``cache/{year}/{month}/{day}/{time}.json.xz``. The filename timestamp is
    treated as the information-availability time for the snapshot.

    The provider emits only flagged / non-default availability states. It does
    not infer an injury from a missed match. It preserves the FPL status,
    chance-of-playing values and current news text as the raw signal.
    """

    environment = "real"

    def __init__(
        self,
        snapshot_root: Path,
        *,
        seasons: Iterable[str] | None = None,
        snapshot_hours: tuple[int, ...] = (0, 6, 12, 18),
    ) -> None:
        self.snapshot_root = snapshot_root
        self._seasons = list(seasons or ["2022-23", "2023-24", "2024-25", "2025-26"])
        self.snapshot_hours = snapshot_hours
        self._cache: dict[str, list[dict[str, Any]]] = {}

    @property
    def provider_name(self) -> str:
        return "fplcache_pit"

    @property
    def source_name(self) -> str:
        return "Randdalf/fplcache point-in-time FPL bootstrap"

    @property
    def seasons_covered(self) -> list[str]:
        return self._seasons

    def snapshots_for_window(
        self,
        start: datetime,
        end: datetime,
    ) -> list[SnapshotRef]:
        """Return locally materialized snapshots within ``[start, end]``."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("snapshot window must be timezone-aware")
        out: list[SnapshotRef] = []
        cursor = start.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        final = end.astimezone(UTC)
        while cursor <= final:
            day_dir = self.snapshot_root / str(cursor.year) / str(cursor.month) / str(cursor.day)
            if day_dir.exists():
                for path in sorted(day_dir.glob("*.json.xz")):
                    try:
                        hour_minute = path.name.removesuffix(".json.xz")
                        captured = cursor.replace(
                            hour=int(hour_minute[:2]),
                            minute=int(hour_minute[2:4]),
                        )
                    except (ValueError, IndexError):
                        continue
                    if start <= captured <= end:
                        out.append(SnapshotRef(captured, path))
            cursor += timedelta(days=1)
        return sorted(out, key=lambda ref: ref.captured_at)

    def latest_before(
        self,
        cutoff: datetime,
        *,
        search_days: int = 2,
    ) -> SnapshotRef | None:
        """Return the latest materialized snapshot not after ``cutoff``."""
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        refs = self.snapshots_for_window(
            cutoff.astimezone(UTC) - timedelta(days=search_days),
            cutoff.astimezone(UTC),
        )
        return refs[-1] if refs else None

    def load_snapshot(self, ref: SnapshotRef) -> dict[str, Any]:
        with lzma.open(ref.path, "rt", encoding="utf-8") as fh:
            value = json.load(fh)
        if not isinstance(value, dict) or not isinstance(value.get("elements"), list):
            raise ValueError(f"invalid fplcache snapshot: {ref.path}")
        return value

    def events_from_snapshot(
        self,
        season: str,
        ref: SnapshotRef,
        *,
        gameweek: int | None = None,
    ) -> list[dict[str, Any]]:
        """Convert one point-in-time bootstrap snapshot into availability events."""
        payload = self.load_snapshot(ref)
        events: list[dict[str, Any]] = []
        captured = ref.captured_at.astimezone(UTC)
        for player in payload["elements"]:
            player_id = str(player.get("id") or "").strip()
            if not player_id:
                continue
            status_code = str(player.get("status") or "a").strip()
            chance_this = _int_or_none(player.get("chance_of_playing_this_round"))
            chance_next = _int_or_none(player.get("chance_of_playing_next_round"))
            news = str(player.get("news") or "").strip()

            if _is_default_available(status_code, chance_this, news):
                continue

            status = _map_status(status_code, chance_this)
            labels = [news, status_code]
            event_type = parse_event_type(labels) if news else _event_type_from_status(status_code)
            description = news or _chance_description(chance_this)
            provider_event_id = f"{captured.isoformat()}:{season}:{player_id}"
            timestamps = AvailabilityTimestamps(
                event_time=None,
                published_at=captured,
                available_at=captured,
                ingested_at=datetime.now(UTC),
            )
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
                    "description": description,
                    "chance_of_playing_this_round": chance_this,
                    "chance_of_playing_next_round": chance_next,
                    "news_added": captured,
                    "timestamps": timestamps,
                    "source_name": self.source_name,
                    "reliability": "official",
                    "snapshot_captured_at": captured,
                    "snapshot_path": ref.path.as_posix(),
                    "snapshot_source_url": f"{FPLCACHE_RAW_BASE}/{ref.path.relative_to(self.snapshot_root).as_posix()}",
                }
            )
        return events

    def fetch_events(self, season: str) -> list[dict[str, Any]]:
        """Fetch all flagged events from every locally available snapshot for a season.

        The season mapping is intentionally calendar-based rather than inferred
        from the current FPL API. This path is primarily used after the ingestion
        script has materialized deadline-adjacent snapshots into ``snapshot_root``.
        """
        if season in self._cache:
            return self._cache[season]
        season_start, season_end = _season_window(season)
        refs = self.snapshots_for_window(season_start, season_end)
        events: list[dict[str, Any]] = []
        for ref in refs:
            events.extend(self.events_from_snapshot(season, ref))
        self._cache[season] = events
        return events


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
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _is_default_available(status_code: str, chance_this: int | None, news: str) -> bool:
    return status_code == "a" and chance_this in (None, 100) and not news


def _map_status(status_code: str, chance_this: int | None) -> str:
    mapping = {
        "a": "available",
        "d": "doubtful",
        "i": "out",
        "s": "suspended",
        "u": "out",
        "n": "unknown",
    }
    status = mapping.get(status_code, "unknown")
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
