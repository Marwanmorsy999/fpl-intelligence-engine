"""Strict chronological eligibility evaluation for PIT availability."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fpl_intelligence.availability.historical.materialize_pit import MaterializeReport
from fpl_intelligence.availability.historical.temporal import AvailabilityTimestamps, classify_temporal, is_event_eligible_before_cutoff
from fpl_intelligence.availability.models import TemporalClass


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
        # Fail closed: no evidence is not 100% coverage.
        return round(self.eligible_before_cutoff / self.total_events, 6) if self.total_events else 0.0

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


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _timestamps(event: dict[str, Any]) -> AvailabilityTimestamps:
    raw = event.get("timestamps")
    if isinstance(raw, AvailabilityTimestamps):
        return raw
    raw = raw if isinstance(raw, dict) else {}
    return AvailabilityTimestamps(
        event_time=_parse_dt(raw.get("event_time")),
        published_at=_parse_dt(raw.get("published_at")),
        available_at=_parse_dt(raw.get("available_at")),
        ingested_at=_parse_dt(raw.get("ingested_at")),
    )


def evaluate_materialize_report(report: MaterializeReport) -> ChronologicalReport:
    out = ChronologicalReport()
    for snapshot in report.snapshots:
        cutoff = snapshot.cutoff.cutoff.astimezone(UTC)
        season = snapshot.cutoff.season_code
        bucket = out.by_season.setdefault(season, {"total": 0, "strict_safe": 0, "eligible": 0, "ineligible": 0, "missing_timestamp": 0})
        for event in snapshot.events:
            ts = _timestamps(event)
            temporal = classify_temporal(ts, strict_backtest_safe=True)
            eligible = temporal == TemporalClass.STRICT_BACKTEST_SAFE and is_event_eligible_before_cutoff(ts, cutoff)
            out.total_events += 1
            bucket["total"] += 1
            if temporal == TemporalClass.STRICT_BACKTEST_SAFE:
                out.strict_safe += 1
                bucket["strict_safe"] += 1
            info_time = ts.published_at or ts.available_at
            if info_time is None:
                out.missing_timestamp += 1
                bucket["missing_timestamp"] += 1
            if eligible:
                out.eligible_before_cutoff += 1
                bucket["eligible"] += 1
            else:
                out.ineligible += 1
                bucket["ineligible"] += 1
                if len(out.sample_ineligible) < 20:
                    out.sample_ineligible.append({
                        "season_code": season,
                        "gameweek": snapshot.cutoff.gameweek,
                        "cutoff": cutoff.isoformat(),
                        "player_id": str(event.get("player_id") or ""),
                        "status": str(event.get("status") or ""),
                        "available_at": info_time.isoformat() if info_time else None,
                        "temporal_class": str(temporal),
                    })
    return out
