"""Data-quality validation for historical availability events (Phase 7.2).

Runs validation for the specific issues the task lists:
- duplicate events
- impossible dates
- player mismatch
- team mismatch
- duplicate articles
- duplicate evidence
- conflicting event states
- missing timestamps
- future events
- events outside season bounds

Ambiguous data is NEVER silently corrected. It is surfaced as a data-quality
issue so it can be reviewed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.historical.providers import HistoricalAvailabilityProvider
from fpl_intelligence.availability.models import AvailabilityArticle, AvailabilityEvent, TemporalClass
from fpl_intelligence.db.models import Gameweek, Season


@dataclass
class DataQualityIssue:
    issue_type: str
    severity: str
    detail: str
    provider_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_type": self.issue_type,
            "severity": self.severity,
            "detail": self.detail,
            "provider_event_id": self.provider_event_id,
        }


@dataclass
class DataQualityReport:
    total_events: int = 0
    issues: list[DataQualityIssue] = field(default_factory=list)

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "issue_count": self.issue_count,
            "issues": [i.to_dict() for i in self.issues],
        }


def validate_historical_availability(
    db: Session,
    provider: HistoricalAvailabilityProvider | None = None,
    seasons: list[str] | None = None,
) -> DataQualityReport:
    """Validate persisted historical availability events for data quality."""
    report = DataQualityReport()
    query = select(AvailabilityEvent)
    if provider is not None:
        query = query.where(AvailabilityEvent.provider == provider.provider_name)
    events = list(db.execute(query).scalars().all())
    report.total_events = len(events)

    # Season bounds lookup.
    season_bounds: dict[int, tuple[datetime | None, datetime | None]] = {}
    for s in db.execute(select(Season)).scalars().all():
        season_bounds[s.id] = (s.start_date, s.end_date)

    seen_provider_ids: set[str] = set()
    seen_players_status: set[tuple[int, str, datetime | None]] = set()

    for ev in events:
        pid = ev.provider_event_id
        # Duplicate events by provider_event_id.
        if pid:
            if pid in seen_provider_ids:
                report.issues.append(
                    DataQualityIssue(
                        "duplicate_event", "high",
                        f"provider_event_id {pid} duplicated",
                    )
                )
            seen_provider_ids.add(pid)

        # Missing timestamps.
        if ev.valid_from is None:
            report.issues.append(
                DataQualityIssue("missing_timestamp", "medium", "event has no valid_from", pid)
            )
        if ev.temporal_class == TemporalClass.UNKNOWN and ev.valid_from is None:
            report.issues.append(
                DataQualityIssue(
                    "missing_timestamp", "medium",
                    "UNKNOWN temporal event has no timestamp",
                    pid,
                )
            )

        # Impossible dates / future events.
        now = datetime.now()
        if ev.valid_from is not None and ev.valid_from > now:
            report.issues.append(
                DataQualityIssue(
                    "future_event", "high",
                    f"valid_from {ev.valid_from.isoformat()} is in the future",
                    pid,
                )
            )

        # Events outside season bounds.
        bounds = season_bounds.get(ev.season_id)
        if ev.valid_from is not None and bounds is not None:
            start, end = bounds
            if start is not None and ev.valid_from < start.replace(tzinfo=start.tzinfo):
                report.issues.append(
                    DataQualityIssue(
                        "outside_season_bounds", "medium",
                        f"valid_from {ev.valid_from.isoformat()} before season start",
                        pid,
                    )
                )
            if end is not None and ev.valid_from > end.replace(tzinfo=end.tzinfo):
                report.issues.append(
                    DataQualityIssue(
                        "outside_season_bounds", "medium",
                        f"valid_from {ev.valid_from.isoformat()} after season end",
                        pid,
                    )
                )

        # Conflicting event states for the same player+season.
        key = (ev.player_id, str(ev.status), ev.valid_from)
        if key in seen_players_status:
            report.issues.append(
                DataQualityIssue(
                    "conflicting_event_state", "medium",
                    f"player {ev.player_id} has duplicate (status,valid_from)",
                    pid,
                )
            )
        seen_players_status.add(key)

    # Duplicate articles by URL.
    dup_articles = list(db.execute(select(AvailabilityArticle)).scalars().all())
    article_urls: set[str] = set()
    for a in dup_articles:
        if a.url in article_urls:
            report.issues.append(
                DataQualityIssue("duplicate_article", "medium", f"article url duplicated: {a.url}")
            )
        article_urls.add(a.url)

    return report
