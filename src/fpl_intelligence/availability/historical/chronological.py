"""Chronological evaluation of point-in-time availability events.

For each deadline-adjacent snapshot event, verify that the information
availability timestamp (published_at / available_at) is at or before the
historical FPL deadline. Events that fail this check must never be treated
as strict pre-deadline intelligence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable

from fpl_intelligence.availability.historical.materialize_pit import (
    DeadlineCutoff,
    MaterializeReport,
)
from fpl_intelligence.availability.historical.temporal import (
    AvailabilityTimestamps,
    classify_temporal,
    is_event_eligible_before_cutoff,
)
from fpl_intelligence.availability.models import TemporalClass


@dataclass
class ChronologicalRow:
    season_code: str
    gameweek: int | None
    cutoff: datetime
    player_id: str
    status: str
    available_at: datetime | None
    temporal_class: str
    eligible_before_cutoff: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "season_code": self.season_code,
            "gameweek": self.gameweek,
            "cutoff": self.cutoff.isoformat(),
            "player_id": self.player_id,
            "status": self.status,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "temporal_class": self.temporal_class,
            "eligible_before_cutoff": self.eligible_before_cutoff,
        }


@dataclass
class ChronologicalReport:
    total_events: int = 0
    strict_safe: int = 0
    eligible_before_cutoff: int = 0
    ineligible: int = 0
    missing_timestamp: int = 0
    by_season: dict[str, dict[str, int]] = field(default_factory=dict)
    sample_ineligible: list[dict[str, Any]] = field(default_factory=list)

    @property
    def eligibility_rate(self) -> float:
        if self.total_events == 0:
            return 1.0
        return round(self.eligible_before_cutoff / self.total_events, 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "strict_safe": self.strict_safe,
            "eligible_before_cutoff": self.eligible_before_cutoff,
            "ineligible": self.ineligible,
            "missing_timestamp": self.missing_timestamp,
            "eligibility_rate": self.eligibility_rate,
            "by_season": self.by_season,
            "sample_ineligible": self.sample_ineligible[:20],
        }


def _ts_from_event(raw: dict[str, Any]) -> AvailabilityTimestamps:
    timestamps = raw.get("timestamps")
    if isinstance(timestamps, AvailabilityTimestamps):
        return timestamps
    if isinstance(timestamps, dict):
        def _p(key: str) -> datetime | None:
            val = timestamps.get(key)
            if val is None or val == "":
                return None
            if isinstance(val, datetime):
                return val if val.tzinfo else val.replace(tzinfo=UTC)
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None

        return AvailabilityTimestamps(
            event_time=_p("event_time"),
            published_at=_p("published_at"),
            available_at=_p("available_at"),
            ingested_at=_p("ingested_at"),
        )
    return AvailabilityTimestamps()


def evaluate_materialize_report(report: MaterializeReport) -> ChronologicalReport:
    """Evaluate chronological integrity of a dry-run materialization."""
    out = ChronologicalReport()
    for snap in report.snapshots:
        cutoff = snap.cutoff.cutoff.astimezone(UTC)
        season = snap.cutoff.season_code
        bucket = out.by_season.setdefault(
            season,
            {
                "total": 0,
                "strict_safe": 0,
                "eligible": 0,
                "ineligible": 0,
                "missing_timestamp": 0,
            },
        )
        for raw in snap.events:
            ts = _ts_from_event(raw)
            temporal = classify_temporal(ts, strict_backtest_safe=True)
            eligible = is_event_eligible_before_cutoff(ts, cutoff)
            out.total_events += 1
            bucket["total"] += 1
            if temporal == TemporalClass.STRICT_BACKTEST_SAFE:
                out.strict_safe += 1
                bucket["strict_safe"] += 1
            info = ts.published_at or ts.available_at
            if info is None:
                out.missing_timestamp += 1
                bucket["missing_timestamp"] += 1
            if eligible:
                out.eligible_before_cutoff += 1
                bucket["eligible"] += 1
            else:
                out.ineligible += 1
                bucket["ineligible"] += 1
                if len(out.sample_ineligible) < 20:
                    out.sample_ineligible.append(
                        ChronologicalRow(
                            season_code=season,
                            gameweek=snap.cutoff.gameweek,
                            cutoff=cutoff,
                            player_id=str(raw.get("player_id") or ""),
                            status=str(raw.get("status") or ""),
                            available_at=info,
                            temporal_class=str(temporal),
                            eligible_before_cutoff=False,
                        ).to_dict()
                    )
    return out


def evaluate_events_against_cutoffs(
    events: Iterable[dict[str, Any]],
    cutoffs: Iterable[DeadlineCutoff],
) -> ChronologicalReport:
    """Evaluate a flat event list against a cutoff list (matched by season+gw)."""
    index: dict[tuple[str, int | None], DeadlineCutoff] = {
        (c.season_code, c.gameweek): c for c in cutoffs
    }
    # Synthetic report shell so we can reuse the row logic.
    from fpl_intelligence.availability.historical.materialize_pit import (
        MaterializedSnapshot,
        MaterializeReport,
    )
    from pathlib import Path

    by_key: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for ev in events:
        key = (str(ev.get("season_code") or ""), ev.get("gameweek"))
        by_key.setdefault(key, []).append(ev)

    report = MaterializeReport()
    for key, evs in by_key.items():
        cutoff = index.get(key)
        if cutoff is None:
            continue
        report.snapshots.append(
            MaterializedSnapshot(
                cutoff=cutoff,
                captured_at=cutoff.cutoff,
                local_path=Path("."),
                source_url="",
                element_count=0,
                flagged_count=len(evs),
                events=evs,
            )
        )
        report.event_count += len(evs)
    return evaluate_materialize_report(report)
