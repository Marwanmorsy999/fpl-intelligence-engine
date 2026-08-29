"""Coverage audit for historical availability events (Phase 7.2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.models import (
    AvailabilityArticle,
    AvailabilityEvent,
    AvailabilityEvidence,
    PlayerInjury,
    PlayerSuspension,
    PressConference,
    TemporalClass,
    TrainingReport,
)
from fpl_intelligence.db.models import Gameweek, PlayerGameweekPerformance, PlayerTeamMembership, Season


@dataclass
class SeasonCoverage:
    season: str
    total_events: int = 0
    strict_safe_events: int = 0
    historical_event_only_events: int = 0
    unknown_events: int = 0
    unique_players: int = 0
    unique_teams: int = 0
    injuries: int = 0
    suspensions: int = 0
    training_reports: int = 0
    press_conferences: int = 0
    articles: int = 0
    evidence_records: int = 0
    unresolved_entities: int = 0
    missing_timestamps: int = 0
    player_gameweeks: int = 0
    player_gameweeks_with_strict_evidence: int = 0

    @property
    def strict_safe_coverage_pct(self) -> float:
        if self.player_gameweeks <= 0:
            return 0.0
        return round(100.0 * self.player_gameweeks_with_strict_evidence / self.player_gameweeks, 1)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "strict_safe_coverage_pct": self.strict_safe_coverage_pct}


@dataclass
class HistoricalCoverageReport:
    seasons: list[str] = field(default_factory=list)
    season_coverage: dict[str, SeasonCoverage] = field(default_factory=dict)
    total_events: int = 0
    total_strict_safe_events: int = 0

    @property
    def strict_safe_event_coverage(self) -> dict[str, float]:
        return {
            code: round(100.0 * cov.strict_safe_events / cov.total_events, 1)
            if cov.total_events > 0 else 0.0
            for code, cov in self.season_coverage.items()
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "seasons": self.seasons,
            "season_coverage": {c: s.to_dict() for c, s in self.season_coverage.items()},
            "total_events": self.total_events,
            "total_strict_safe_events": self.total_strict_safe_events,
            "strict_safe_event_coverage": self.strict_safe_event_coverage,
        }


def audit_historical_coverage(db: Session, seasons: list[str] | None = None, *, exclude_mock: bool = True) -> HistoricalCoverageReport:
    """Audit real historical availability coverage without inflating strict player-GW counts."""
    report = HistoricalCoverageReport()
    if seasons is None:
        seasons = list(db.execute(select(Season.code).order_by(Season.code)).scalars().all())
    report.seasons = list(seasons)

    for code in seasons:
        season = db.scalar(select(Season).where(Season.code == code))
        if season is None:
            continue
        sid = season.id
        events = list(db.execute(select(AvailabilityEvent).where(AvailabilityEvent.season_id == sid)).scalars().all())
        if exclude_mock:
            events = [e for e in events if e.provider != "sample"]

        gw_ids = list(db.scalars(select(Gameweek.id).where(Gameweek.season_id == sid)).all())
        player_gws = (
            db.scalar(select(func.count()).select_from(PlayerGameweekPerformance).where(PlayerGameweekPerformance.gameweek_id.in_(gw_ids))) or 0
        ) if gw_ids else 0

        strict_safe = hist_only = unknown = missing_ts = 0
        strict_player_gws: set[tuple[int, int]] = set()
        for ev in events:
            if ev.temporal_class == TemporalClass.STRICT_BACKTEST_SAFE:
                strict_safe += 1
                if ev.gameweek_id is not None:
                    strict_player_gws.add((ev.player_id, ev.gameweek_id))
            elif ev.temporal_class == TemporalClass.HISTORICAL_EVENT_ONLY:
                hist_only += 1
            else:
                unknown += 1
            if ev.valid_from is None:
                missing_ts += 1

        players = {ev.player_id for ev in events}
        teams = set()
        for ev in events:
            membership = db.scalar(select(PlayerTeamMembership).where(
                PlayerTeamMembership.player_id == ev.player_id,
                PlayerTeamMembership.season_id == sid,
            ))
            if membership is not None:
                teams.add(membership.team_id)

        injuries = db.scalar(select(func.count()).select_from(PlayerInjury).join(
            PlayerGameweekPerformance, PlayerGameweekPerformance.player_id == PlayerInjury.player_id
        ).where(PlayerGameweekPerformance.season_id == sid)) or 0
        suspensions = db.scalar(select(func.count()).select_from(PlayerSuspension).where(PlayerSuspension.season_id == sid)) or 0
        training = db.scalar(select(func.count()).select_from(TrainingReport).join(
            PlayerGameweekPerformance, PlayerGameweekPerformance.player_id == TrainingReport.player_id
        ).where(PlayerGameweekPerformance.season_id == sid)) or 0
        press_conf = db.scalar(select(func.count()).select_from(PressConference).where(PressConference.season_id == sid)) or 0
        articles = db.scalar(select(func.count()).select_from(AvailabilityArticle)) or 0
        evidence_count = db.scalar(select(func.count()).select_from(AvailabilityEvidence)) or 0

        cov = SeasonCoverage(
            season=code,
            total_events=len(events),
            strict_safe_events=strict_safe,
            historical_event_only_events=hist_only,
            unknown_events=unknown,
            unique_players=len(players),
            unique_teams=len(teams),
            injuries=injuries,
            suspensions=suspensions,
            training_reports=training,
            press_conferences=press_conf,
            articles=articles,
            evidence_records=evidence_count,
            missing_timestamps=missing_ts,
            player_gameweeks=player_gws,
            player_gameweeks_with_strict_evidence=len(strict_player_gws),
        )
        report.season_coverage[code] = cov
        report.total_events += len(events)
        report.total_strict_safe_events += strict_safe

    return report
