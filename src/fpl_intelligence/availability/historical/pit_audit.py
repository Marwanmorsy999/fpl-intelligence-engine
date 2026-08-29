"""Reproducible validation audit for imported PIT availability events."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.models import AvailabilityEvent, TemporalClass
from fpl_intelligence.db.models import PlayerGameweekPerformance, Season

_RESTRICTED = {"out", "suspended", "doubtful", "questionable", "suspect"}
_HARD_OUT = {"out", "suspended"}


@dataclass
class PITAuditReport:
    provider: str = "fplcache_pit"
    seasons: list[str] = field(default_factory=list)
    event_count: int = 0
    strict_safe: int = 0
    timestamp_complete: int = 0
    gameweek_linked: int = 0
    performance_matches: int = 0
    restricted_rows: int = 0
    hard_out_rows: int = 0
    hard_out_mean_minutes: float | None = None
    restricted_mean_minutes: float | None = None
    restricted_start_rate: float | None = None
    by_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def chronology_rate(self) -> float:
        return self.timestamp_complete / self.event_count if self.event_count else 0.0

    @property
    def hard_out_signal_ok(self) -> bool:
        return (
            self.hard_out_rows >= 10
            and self.hard_out_mean_minutes is not None
            and self.hard_out_mean_minutes <= 5.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "chronology_rate": round(self.chronology_rate, 6),
            "hard_out_signal_ok": self.hard_out_signal_ok,
        }


def audit_pit_events(db: Session, seasons: list[str] | None = None) -> PITAuditReport:
    """Audit PIT events and realized player-GW minutes in the validation DB."""
    query = select(AvailabilityEvent).where(AvailabilityEvent.provider == "fplcache_pit")
    if seasons:
        season_rows = list(db.execute(select(Season).where(Season.code.in_(seasons))).scalars().all())
        season_ids = [season.id for season in season_rows]
        query = query.where(AvailabilityEvent.season_id.in_(season_ids)) if season_ids else query.where(False)

    events = list(db.execute(query).scalars().all())
    report = PITAuditReport(seasons=seasons or [])
    report.event_count = len(events)
    report.strict_safe = sum(e.temporal_class == TemporalClass.STRICT_BACKTEST_SAFE for e in events)
    report.timestamp_complete = sum(e.valid_from is not None for e in events)
    report.gameweek_linked = sum(e.gameweek_id is not None for e in events)

    restricted_minutes: list[float] = []
    hard_out_minutes: list[float] = []
    status_minutes: dict[str, list[float]] = {}

    for event in events:
        if event.gameweek_id is None:
            continue
        perf = db.scalar(select(PlayerGameweekPerformance).where(
            PlayerGameweekPerformance.player_id == event.player_id,
            PlayerGameweekPerformance.gameweek_id == event.gameweek_id,
        ))
        if perf is None:
            continue
        minutes = float(perf.minutes or 0)
        report.performance_matches += 1
        status = str(event.status.value if hasattr(event.status, "value") else event.status).lower()
        status_minutes.setdefault(status, []).append(minutes)
        if status in _RESTRICTED:
            restricted_minutes.append(minutes)
        if status in _HARD_OUT:
            hard_out_minutes.append(minutes)

    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    def _rate(values: list[float]) -> float | None:
        return round(sum(value >= 60 for value in values) / len(values), 6) if values else None

    report.restricted_rows = len(restricted_minutes)
    report.hard_out_rows = len(hard_out_minutes)
    report.restricted_mean_minutes = _mean(restricted_minutes)
    report.hard_out_mean_minutes = _mean(hard_out_minutes)
    report.restricted_start_rate = _rate(restricted_minutes)
    report.by_status = {
        status: {"n": len(values), "mean_minutes": _mean(values), "start_rate": _rate(values)}
        for status, values in sorted(status_minutes.items())
    }

    if report.event_count == 0:
        report.notes.append("No fplcache_pit events found.")
    if report.event_count and report.strict_safe != report.event_count:
        report.notes.append("Not every PIT event is classified STRICT_BACKTEST_SAFE.")
    if report.event_count and report.timestamp_complete != report.event_count:
        report.notes.append("Some PIT events are missing valid_from information-availability timestamps.")
    if report.event_count and report.gameweek_linked != report.event_count:
        report.notes.append("Some PIT events are not linked to a gameweek.")
    if report.hard_out_signal_ok:
        report.notes.append("Hard-out statuses show near-zero realized minutes on the validation sample.")
    else:
        report.notes.append("Hard-out signal is not yet established on the available validation sample.")
    return report
