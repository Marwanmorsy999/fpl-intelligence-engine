"""Materialize point-in-time fplcache snapshots for Phase 7 import.

Downloads only deadline-adjacent immutable bootstrap snapshots from
Randdalf/fplcache, writes them under a local cache root, converts flagged
players into availability events, and optionally persists them via the
existing historical importer.

Design constraints:
- event time ≠ information availability time (snapshot captured_at is available_at)
- never load an entire season of 6-hour snapshots by default
- dry-run is the default path (no DB writes)
- append-only / idempotent import when --import is used
"""

from __future__ import annotations

import json
import logging
import lzma
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from fpl_intelligence.availability.historical.pit_fplcache import (
    FPLCACHE_RAW_BASE,
    PointInTimeFPLCacheAvailabilityProvider,
    SnapshotRef,
)

logger = logging.getLogger(__name__)

FPLCACHE_API = "https://api.github.com/repos/Randdalf/fplcache/contents/cache"
USER_AGENT = "fpl-intelligence-engine-pit-materialize"


@dataclass(frozen=True)
class DeadlineCutoff:
    """One historical decision boundary and its season/gameweek context."""

    season_code: str
    gameweek: int | None
    cutoff: datetime

    def __post_init__(self) -> None:
        if self.cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")


@dataclass
class MaterializedSnapshot:
    cutoff: DeadlineCutoff
    captured_at: datetime
    local_path: Path
    source_url: str
    element_count: int
    flagged_count: int
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MaterializeReport:
    snapshots: list[MaterializedSnapshot] = field(default_factory=list)
    event_count: int = 0
    downloaded: int = 0
    reused: int = 0
    missing: int = 0
    dry_run: bool = True
    import_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": "fplcache_pit",
            "dry_run": self.dry_run,
            "downloaded": self.downloaded,
            "reused": self.reused,
            "missing": self.missing,
            "event_count": self.event_count,
            "snapshots": [
                {
                    "season_code": s.cutoff.season_code,
                    "gameweek": s.cutoff.gameweek,
                    "cutoff": s.cutoff.cutoff.isoformat(),
                    "snapshot_captured_at": s.captured_at.isoformat(),
                    "local_path": s.local_path.as_posix(),
                    "source_url": s.source_url,
                    "elements": s.element_count,
                    "flagged_players": s.flagged_count,
                }
                for s in self.snapshots
            ],
            "import_result": self.import_result,
        }


def _github_json(url: str) -> object:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _snapshot_files(day: date) -> list[tuple[datetime, str]]:
    url = f"{FPLCACHE_API}/{day.year}/{day.month}/{day.day}"
    try:
        payload = _github_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    if not isinstance(payload, list):
        return []
    result: list[tuple[datetime, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name.endswith(".json.xz"):
            continue
        stem = name.removesuffix(".json.xz")
        if len(stem) != 4 or not stem.isdigit():
            continue
        try:
            captured = datetime(
                day.year,
                day.month,
                day.day,
                int(stem[:2]),
                int(stem[2:]),
                tzinfo=UTC,
            )
        except ValueError:
            continue
        result.append(
            (
                captured,
                f"{FPLCACHE_RAW_BASE}/{day.year}/{day.month}/{day.day}/{name}",
            )
        )
    return sorted(result)


def latest_remote_before(cutoff: datetime, *, search_days: int = 3) -> tuple[datetime, str] | None:
    """Find the latest public fplcache snapshot at or before cutoff."""
    cutoff_utc = cutoff.astimezone(UTC)
    candidates: list[tuple[datetime, str]] = []
    for offset in range(search_days + 1):
        day = (cutoff_utc - timedelta(days=offset)).date()
        candidates.extend(item for item in _snapshot_files(day) if item[0] <= cutoff_utc)
    return max(candidates, key=lambda item: item[0]) if candidates else None


def local_snapshot_path(root: Path, captured_at: datetime) -> Path:
    """Canonical on-disk path matching PointInTimeFPLCacheAvailabilityProvider."""
    captured = captured_at.astimezone(UTC)
    return (
        root
        / str(captured.year)
        / str(captured.month)
        / str(captured.day)
        / f"{captured:%H%M}.json.xz"
    )


def download_snapshot(url: str, dest: Path) -> None:
    """Download an immutable compressed snapshot to dest (parents created)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as response:
        data = response.read()
    # Validate payload before writing.
    payload = json.loads(lzma.decompress(data).decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError(f"invalid fplcache snapshot payload: {url}")
    dest.write_bytes(data)


def ensure_snapshot(
    root: Path,
    cutoff: datetime,
    *,
    search_days: int = 3,
    force: bool = False,
) -> tuple[SnapshotRef, str, bool] | None:
    """Ensure a local snapshot exists for the latest remote capture ≤ cutoff.

    Returns (SnapshotRef, source_url, downloaded_now) or None if no remote
    snapshot exists in the search window.
    """
    remote = latest_remote_before(cutoff, search_days=search_days)
    if remote is None:
        return None
    captured, url = remote
    path = local_snapshot_path(root, captured)
    downloaded = False
    if force or not path.exists():
        download_snapshot(url, path)
        downloaded = True
    return SnapshotRef(captured, path), url, downloaded


def materialize_cutoffs(
    root: Path,
    cutoffs: Iterable[DeadlineCutoff],
    *,
    search_days: int = 3,
    force: bool = False,
) -> MaterializeReport:
    """Download deadline-adjacent snapshots and emit availability events."""
    provider = PointInTimeFPLCacheAvailabilityProvider(root)
    report = MaterializeReport(dry_run=True)
    for cutoff in cutoffs:
        ensured = ensure_snapshot(
            root, cutoff.cutoff, search_days=search_days, force=force
        )
        if ensured is None:
            report.missing += 1
            logger.warning(
                "no fplcache snapshot on or before %s (season=%s gw=%s)",
                cutoff.cutoff.isoformat(),
                cutoff.season_code,
                cutoff.gameweek,
            )
            continue
        ref, url, downloaded = ensured
        if downloaded:
            report.downloaded += 1
        else:
            report.reused += 1
        events = provider.events_from_snapshot(
            cutoff.season_code, ref, gameweek=cutoff.gameweek
        )
        payload = provider.load_snapshot(ref)
        snap = MaterializedSnapshot(
            cutoff=cutoff,
            captured_at=ref.captured_at,
            local_path=ref.path,
            source_url=url,
            element_count=len(payload["elements"]),
            flagged_count=len(events),
            events=events,
        )
        report.snapshots.append(snap)
        report.event_count += len(events)
    return report


def collect_events(report: MaterializeReport) -> list[dict[str, Any]]:
    """Flatten all events from a materialization report."""
    out: list[dict[str, Any]] = []
    for snap in report.snapshots:
        out.extend(snap.events)
    return out


class _StaticEventProvider:
    """Thin adapter so the existing importer can consume pre-built events."""

    environment = "real"

    def __init__(self, events_by_season: dict[str, list[dict[str, Any]]]) -> None:
        self._events = events_by_season

    @property
    def provider_name(self) -> str:
        return "fplcache_pit"

    @property
    def source_name(self) -> str:
        return "Randdalf/fplcache point-in-time FPL bootstrap"

    @property
    def seasons_covered(self) -> list[str]:
        return sorted(self._events)

    def fetch_events(self, season: str) -> list[dict[str, Any]]:
        return list(self._events.get(season, []))


def import_materialized(
    db: Any,
    report: MaterializeReport,
    *,
    strict_backtest_safe: bool = True,
) -> dict[str, Any]:
    """Persist materialized events via the Phase 7 historical importer."""
    from fpl_intelligence.availability.historical.importer import (
        import_historical_availability,
    )
    from fpl_intelligence.availability.historical.providers import (
        HistoricalAvailabilityProvider,
    )

    by_season: dict[str, list[dict[str, Any]]] = {}
    for snap in report.snapshots:
        by_season.setdefault(snap.cutoff.season_code, []).extend(snap.events)

    # Runtime structural check: adapter matches the importer protocol.
    provider: HistoricalAvailabilityProvider = _StaticEventProvider(by_season)  # type: ignore[assignment]
    result = import_historical_availability(
        db,
        provider,
        list(by_season.keys()),
        strict_backtest_safe=strict_backtest_safe,
    )
    report.dry_run = False
    report.import_result = result.to_dict()
    return report.import_result
