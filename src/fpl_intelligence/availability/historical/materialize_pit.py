"""Materialize immutable fplcache snapshots at explicit historical deadlines.

The default workflow is dry-run. Database persistence is delegated to the existing
Phase 7 importer and must be explicitly requested by the caller.
"""
from __future__ import annotations

import json
import lzma
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from fpl_intelligence.availability.historical.pit_fplcache import (
    FPLCACHE_API if False else FPLCACHE_RAW_BASE,  # type: ignore[misc]
    PointInTimeFPLCacheAvailabilityProvider,
    SnapshotRef,
)

# GitHub contents endpoint used only for read-only snapshot discovery.
FPLCACHE_API = "https://api.github.com/repos/Randdalf/fplcache/contents/cache"
USER_AGENT = "fpl-intelligence-engine-pit-materialize"


@dataclass(frozen=True)
class DeadlineCutoff:
    season_code: str
    gameweek: int | None
    cutoff: datetime

    def __post_init__(self) -> None:
        if self.cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        object.__setattr__(self, "cutoff", self.cutoff.astimezone(UTC))


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
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _snapshot_files(day: date) -> list[tuple[datetime, str]]:
    try:
        payload = _github_json(f"{FPLCACHE_API}/{day.year}/{day.month}/{day.day}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    if not isinstance(payload, list):
        return []
    out: list[tuple[datetime, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        stem = name.removesuffix(".json.xz")
        if not name.endswith(".json.xz") or len(stem) != 4 or not stem.isdigit():
            continue
        try:
            captured = datetime(day.year, day.month, day.day, int(stem[:2]), int(stem[2:]), tzinfo=UTC)
        except ValueError:
            continue
        out.append((captured, f"{FPLCACHE_RAW_BASE}/{day.year}/{day.month}/{day.day}/{name}"))
    return sorted(out)


def latest_remote_before(cutoff: datetime, *, search_days: int = 3) -> tuple[datetime, str] | None:
    """Return the latest remote snapshot whose capture time is <= cutoff."""
    cutoff_utc = cutoff.astimezone(UTC)
    candidates: list[tuple[datetime, str]] = []
    for offset in range(max(0, search_days) + 1):
        day = (cutoff_utc - timedelta(days=offset)).date()
        candidates.extend(item for item in _snapshot_files(day) if item[0] <= cutoff_utc)
    return max(candidates, key=lambda x: x[0]) if candidates else None


def local_snapshot_path(root: Path, captured_at: datetime) -> Path:
    captured = captured_at.astimezone(UTC)
    return root / str(captured.year) / str(captured.month) / str(captured.day) / f"{captured:%H%M}.json.xz"


def download_snapshot(url: str, dest: Path) -> None:
    """Download and validate an immutable compressed JSON snapshot before writing."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as response:
        data = response.read()
    payload = json.loads(lzma.decompress(data).decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError(f"invalid fplcache snapshot payload: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def ensure_snapshot(root: Path, cutoff: datetime, *, search_days: int = 3, force: bool = False) -> tuple[SnapshotRef, str, bool] | None:
    remote = latest_remote_before(cutoff, search_days=search_days)
    if remote is None:
        return None
    captured, url = remote
    path = local_snapshot_path(root, captured)
    downloaded = force or not path.exists()
    if downloaded:
        download_snapshot(url, path)
    return SnapshotRef(captured, path), url, downloaded


def materialize_cutoffs(root: Path, cutoffs: Iterable[DeadlineCutoff], *, search_days: int = 3, force: bool = False) -> MaterializeReport:
    provider = PointInTimeFPLCacheAvailabilityProvider(root)
    report = MaterializeReport()
    for cutoff in cutoffs:
        ensured = ensure_snapshot(root, cutoff.cutoff, search_days=search_days, force=force)
        if ensured is None:
            report.missing += 1
            continue
        ref, url, downloaded = ensured
        report.downloaded += int(downloaded)
        report.reused += int(not downloaded)
        payload = provider.load_snapshot(ref)
        events = provider.events_from_snapshot(cutoff.season_code, ref, gameweek=cutoff.gameweek)
        report.snapshots.append(MaterializedSnapshot(cutoff, ref.captured_at, ref.path, url, len(payload["elements"]), len(events), events))
        report.event_count += len(events)
    return report


def collect_events(report: MaterializeReport) -> list[dict[str, Any]]:
    return [event for snapshot in report.snapshots for event in snapshot.events]


class _StaticEventProvider:
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


def import_materialized(db: Any, report: MaterializeReport, *, strict_backtest_safe: bool = True) -> dict[str, Any]:
    """Import through the canonical append-only Phase 7 historical importer."""
    from fpl_intelligence.availability.historical.importer import import_historical_availability

    by_season: dict[str, list[dict[str, Any]]] = {}
    for snapshot in report.snapshots:
        by_season.setdefault(snapshot.cutoff.season_code, []).extend(snapshot.events)
    provider = _StaticEventProvider(by_season)
    result = import_historical_availability(db, provider, list(by_season), strict_backtest_safe=strict_backtest_safe)
    report.dry_run = False
    report.import_result = result.to_dict()
    return report.import_result
