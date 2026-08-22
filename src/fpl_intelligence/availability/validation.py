"""Availability coverage and temporal-eligibility audits for Phase 7.

These audits run against the populated database and produce honest,
machine-readable reports. They never substitute synthetic values for missing
availability data, and they never assume coverage is complete.

If the Phase 7 availability tables are empty (e.g. only the structured FPL
mirror has been imported), the audits report zero coverage and the temporal
audit reports zero eligible events -- enabling the runner to conclude that
Phase 7 empirical validation is BLOCKED for lack of historical availability
data, rather than fabricating a BASELINE == PHASE7 result as a meaningful
experiment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.models import (
    AvailabilityArticle,
    AvailabilityEvent,
    AvailabilityEvidence,
    AvailabilitySource,
    PlayerInjury,
    PlayerMention,
    PlayerSuspension,
    PressConference,
    TrainingReport,
)
from fpl_intelligence.db.models import (
    Gameweek,
    PlayerGameweekPerformance,
    Season,
)


@dataclass
class AvailabilitySeasonCoverage:
    """Per-season availability intelligence coverage."""

    season: str
    availability_events: int = 0
    injuries: int = 0
    suspensions: int = 0
    training_reports: int = 0
    press_conferences: int = 0
    player_mentions: int = 0
    evidence_records: int = 0
    articles: int = 0
    sources: int = 0
    player_gameweeks: int = 0
    player_gameweeks_with_evidence: int = 0

    @property
    def coverage_pct(self) -> float:
        """Fraction of player-gameweeks with at least one availability event."""
        if self.player_gameweeks <= 0:
            return 0.0
        return round(100.0 * self.player_gameweeks_with_evidence / self.player_gameweeks, 1)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "coverage_pct": self.coverage_pct}


@dataclass
class AvailabilityCoverageReport:
    """Aggregate availability coverage across seasons."""

    seasons: list[str] = field(default_factory=list)
    season_coverage: dict[str, AvailabilitySeasonCoverage] = field(default_factory=dict)
    total_events: int = 0
    total_evidence: int = 0
    total_player_gameweeks: int = 0
    total_player_gameweeks_with_evidence: int = 0

    @property
    def overall_coverage_pct(self) -> float:
        if self.total_player_gameweeks <= 0:
            return 0.0
        return round(
            100.0 * self.total_player_gameweeks_with_evidence / self.total_player_gameweeks,
            1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seasons": self.seasons,
            "season_coverage": {s: c.to_dict() for s, c in self.season_coverage.items()},
            "total_events": self.total_events,
            "total_evidence": self.total_evidence,
            "total_player_gameweeks": self.total_player_gameweeks,
            "total_player_gameweeks_with_evidence": self.total_player_gameweeks_with_evidence,
            "overall_coverage_pct": self.overall_coverage_pct,
        }


def audit_availability_coverage(
    db: Session, seasons: list[str] | None = None
) -> AvailabilityCoverageReport:
    """Audit availability-intelligence coverage for the given seasons.

    Args:
        db: Database session.
        seasons: Season codes to audit. Defaults to all seasons in the DB.
    """
    report = AvailabilityCoverageReport()
    if seasons is None:
        seasons = list(db.execute(select(Season.code).order_by(Season.code)).scalars().all())
    report.seasons = list(seasons)

    for code in seasons:
        season = db.scalar(select(Season).where(Season.code == code))
        if season is None:
            continue
        sid = season.id

        gw_ids = list(db.scalars(select(Gameweek.id).where(Gameweek.season_id == sid)).all())
        player_gws = 0
        if gw_ids:
            player_gws = (
                db.scalar(
                    select(func.count())
                    .select_from(PlayerGameweekPerformance)
                    .where(PlayerGameweekPerformance.gameweek_id.in_(gw_ids))
                )
                or 0
            )

        event_player_gws = set(
            db.scalars(
                select(AvailabilityEvent.player_id).where(AvailabilityEvent.season_id == sid)
            ).all()
        )
        # Player-gameweeks with evidence: distinct (player_id) among events is
        # used as a conservative proxy because evidence rows may not carry a
        # resolved gameweek. This is an honest lower bound on coverage.
        with_evidence = len(event_player_gws)

        cov = AvailabilitySeasonCoverage(
            season=code,
            availability_events=db.scalar(
                select(func.count())
                .select_from(AvailabilityEvent)
                .where(AvailabilityEvent.season_id == sid)
            )
            or 0,
            injuries=db.scalar(
                select(func.count())
                .select_from(PlayerInjury)
                .join(
                    PlayerGameweekPerformance,
                    PlayerGameweekPerformance.player_id == PlayerInjury.player_id,
                )
                .where(PlayerGameweekPerformance.season_id == sid)
            )
            or 0,
            suspensions=db.scalar(
                select(func.count())
                .select_from(PlayerSuspension)
                .where(PlayerSuspension.season_id == sid)
            )
            or 0,
            training_reports=db.scalar(
                select(func.count())
                .select_from(TrainingReport)
                .join(
                    PlayerGameweekPerformance,
                    PlayerGameweekPerformance.player_id == TrainingReport.player_id,
                )
                .where(PlayerGameweekPerformance.season_id == sid)
            )
            or 0,
            press_conferences=db.scalar(
                select(func.count())
                .select_from(PressConference)
                .where(PressConference.season_id == sid)
            )
            or 0,
            player_mentions=db.scalar(
                select(func.count())
                .select_from(PlayerMention)
                .join(
                    PressConference,
                    PressConference.id == PlayerMention.press_conference_id,
                )
                .where(PressConference.season_id == sid)
            )
            or 0,
            evidence_records=db.scalar(
                select(func.count())
                .select_from(AvailabilityEvidence)
                .join(Season, Season.id == sid)
            )
            or 0,
            articles=db.scalar(select(func.count()).select_from(AvailabilityArticle)) or 0,
            sources=db.scalar(select(func.count()).select_from(AvailabilitySource)) or 0,
            player_gameweeks=player_gws,
            player_gameweeks_with_evidence=with_evidence,
        )
        report.season_coverage[code] = cov
        report.total_events += cov.availability_events
        report.total_evidence += cov.evidence_records
        report.total_player_gameweeks += player_gws
        report.total_player_gameweeks_with_evidence += with_evidence

    return report


@dataclass
class TemporalAvailabilityReport:
    """Temporal-eligibility audit for availability events."""

    total_events: int = 0
    eligible_events: int = 0
    excluded_future_events: int = 0
    missing_timestamp_events: int = 0
    excluded_ambiguous_events: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


def audit_temporal_availability(db: Session) -> TemporalAvailabilityReport:
    """Audit temporal eligibility of availability events.

    Uses the strict reproducibility policy: an event is eligible only if its
    ``valid_from`` is well-defined and <= the deadline of the gameweek it
    references (or the season's latest deadline when no gameweek is linked).
    Events that became available after the simulated decision cutoff are
    excluded (future events). Events lacking a usable timestamp are counted as
    missing-timestamp. Ambiguous events (no season/gameweek linkage to anchor a
    cutoff) are counted as ambiguous and excluded from strict eligibility.
    """
    report = TemporalAvailabilityReport()
    events = list(db.execute(select(AvailabilityEvent)).scalars().all())
    report.total_events = len(events)

    # Map season -> latest gameweek deadline to anchor events without a GW link.
    season_deadlines: dict[int, Any] = {}
    for gw in db.execute(select(Gameweek)).scalars().all():
        cur = season_deadlines.get(gw.season_id)
        if gw.deadline_time is not None and (cur is None or gw.deadline_time > cur):
            season_deadlines[gw.season_id] = gw.deadline_time

    season_code: dict[int, str] = {}
    for s in db.execute(select(Season)).scalars().all():
        season_code[s.id] = s.code

    for ev in events:
        detail: dict[str, Any] = {
            "event_id": ev.id,
            "player_id": ev.player_id,
            "status": ev.status,
            "valid_from": ev.valid_from.isoformat() if ev.valid_from else None,
            "season": season_code.get(ev.season_id),
            "gameweek_id": ev.gameweek_id,
        }

        # Missing timestamp.
        if ev.valid_from is None:
            report.missing_timestamp_events += 1
            report.excluded_ambiguous_events += 1
            detail["eligibility"] = "missing_timestamp"
            report.details.append(detail)
            continue

        # Determine the decision cutoff to compare against.
        cutoff = None
        if ev.gameweek_id is not None:
            gw_row = db.get(Gameweek, ev.gameweek_id)
            if gw_row is not None and gw_row.deadline_time is not None:
                cutoff = gw_row.deadline_time
        if cutoff is None:
            # Anchor to the season's latest deadline (conservative).
            cutoff = season_deadlines.get(ev.season_id)

        if cutoff is None:
            report.excluded_ambiguous_events += 1
            detail["eligibility"] = "ambiguous_no_cutoff"
            report.details.append(detail)
            continue

        # Future event: became available after the decision cutoff.
        if ev.valid_from > cutoff:
            report.excluded_future_events += 1
            detail["eligibility"] = "future_event"
            report.details.append(detail)
            continue

        report.eligible_events += 1
        detail["eligibility"] = "eligible"
        report.details.append(detail)

    return report
