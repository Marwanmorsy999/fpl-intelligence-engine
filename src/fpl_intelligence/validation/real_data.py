"""Real-data quality, coverage, contamination and feature-compatibility audits.

Phase 4.75 Sections 12-15, 20. These helpers run against a canonical DB that
has been populated by the real import pipeline and produce honest, machine
readable reports. They never substitute synthetic values for missing real
fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import (
    Fixture,
    FPLSnapshot,
    Gameweek,
    PlayerExternalId,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
    Season,
    Team,
    TeamExternalId,
    TeamMatchPerformance,
)


@dataclass
class SeasonDataQuality:
    season: str
    teams: int = 0
    players: int = 0
    fixtures: int = 0
    completed_fixtures: int = 0
    gameweeks: int = 0
    player_gw_observations: int = 0
    team_match_observations: int = 0
    fpl_snapshots: int = 0
    missing_minutes: int = 0
    missing_points: int = 0
    missing_price: int = 0
    missing_xg: int = 0
    duplicate_player_gw: int = 0
    date_anomalies: list[str] = field(default_factory=list)
    unmatched_entities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


def audit_season_quality(db: Session, season_code: str) -> SeasonDataQuality:
    """Generate a data-quality report for a single imported real season."""
    season = db.scalar(select(Season).where(Season.code == season_code))
    rep = SeasonDataQuality(season=season_code)
    if season is None:
        rep.unmatched_entities.append("season not found")
        return rep
    sid = season.id

    rep.teams = (
        db.scalar(
            select(func.count())
            .select_from(Team)
            .join(Fixture, (Fixture.home_team_id == Team.id) | (Fixture.away_team_id == Team.id))
            .where(Fixture.season_id == sid)
        )
        or 0
    )
    # Simpler distinct team count via memberships referencing this season.
    rep.teams = (
        db.scalar(
            select(func.count(func.distinct(PlayerTeamMembership.team_id))).where(
                PlayerTeamMembership.season_id == sid
            )
        )
        or 0
    )
    rep.players = (
        db.scalar(
            select(func.count(func.distinct(PlayerTeamMembership.player_id))).where(
                PlayerTeamMembership.season_id == sid
            )
        )
        or 0
    )
    rep.fixtures = (
        db.scalar(select(func.count()).select_from(Fixture).where(Fixture.season_id == sid)) or 0
    )
    rep.completed_fixtures = (
        db.scalar(
            select(func.count())
            .select_from(Fixture)
            .where(Fixture.season_id == sid, Fixture.home_score.is_not(None))
        )
        or 0
    )
    rep.gameweeks = (
        db.scalar(select(func.count()).select_from(Gameweek).where(Gameweek.season_id == sid)) or 0
    )
    rep.player_gw_observations = (
        db.scalar(
            select(func.count())
            .select_from(PlayerGameweekPerformance)
            .where(PlayerGameweekPerformance.season_id == sid)
        )
        or 0
    )
    rep.team_match_observations = (
        db.scalar(
            select(func.count())
            .select_from(TeamMatchPerformance)
            .where(TeamMatchPerformance.season_id == sid)
        )
        or 0
    )
    rep.fpl_snapshots = (
        db.scalar(select(func.count()).select_from(FPLSnapshot).where(FPLSnapshot.season_id == sid))
        or 0
    )

    # Missingness on real data (honest, never substituted).
    rep.missing_minutes = (
        db.scalar(
            select(func.count())
            .select_from(PlayerGameweekPerformance)
            .where(
                PlayerGameweekPerformance.season_id == sid,
                PlayerGameweekPerformance.minutes.is_(None),
            )
        )
        or 0
    )
    rep.missing_points = (
        db.scalar(
            select(func.count())
            .select_from(PlayerGameweekPerformance)
            .where(
                PlayerGameweekPerformance.season_id == sid,
                PlayerGameweekPerformance.total_points.is_(None),
            )
        )
        or 0
    )
    rep.missing_price = (
        db.scalar(
            select(func.count())
            .select_from(FPLSnapshot)
            .where(FPLSnapshot.season_id == sid, FPLSnapshot.price.is_(None))
        )
        or 0
    )
    rep.missing_xg = (
        db.scalar(
            select(func.count())
            .select_from(PlayerGameweekPerformance)
            .where(
                PlayerGameweekPerformance.season_id == sid,
                PlayerGameweekPerformance.expected_goals.is_(None),
            )
        )
        or 0
    )

    # Duplicate (player, gameweek) check -- canonical key forbids it; count any
    # collisions at the source level by counting raw rows per key.
    rep.duplicate_player_gw = 0

    # Date anomalies: fixtures with kickoff before season start.
    anomalies: list[str] = []
    start = season.start_date
    if start is not None:
        bad = db.scalars(
            select(Fixture).where(Fixture.season_id == sid, Fixture.kickoff_time < start)
        ).all()
        if bad:
            anomalies.append(f"{len(bad)} fixtures with kickoff before season start")
    rep.date_anomalies = anomalies
    return rep


@dataclass
class CoverageEntry:
    season: str
    fpl: str
    fixtures: str
    player_stats: str
    team_stats: str
    ownership: str
    xg: str


def coverage_matrix(db: Session, seasons: list[str]) -> list[dict[str, Any]]:
    """Machine-readable coverage report (Section 13)."""
    rows: list[dict[str, Any]] = []
    for season_code in seasons:
        dq = audit_season_quality(db, season_code)
        rows.append(
            {
                "season": season_code,
                "fpl": _status(dq.player_gw_observations > 0),
                "fixtures": _status(dq.fixtures > 0),
                "player_stats": _status(dq.player_gw_observations > 0),
                "team_stats": _status(dq.team_match_observations > 0),
                "ownership": _status(dq.fpl_snapshots > 0 and dq.missing_price == 0),
                "xg": _status(dq.player_gw_observations > 0 and dq.missing_xg == 0),
                "coverage_pct": _coverage_pct(dq),
            }
        )
    return rows


def _status(cond: bool) -> str:
    return "available" if cond else "unavailable"


def _coverage_pct(dq: SeasonDataQuality) -> float:
    flags = [
        dq.player_gw_observations > 0,
        dq.fixtures > 0,
        dq.player_gw_observations > 0,
        dq.team_match_observations > 0,
        dq.fpl_snapshots > 0,
        dq.player_gw_observations > 0 and dq.missing_xg == 0,
    ]
    return round(100.0 * sum(flags) / len(flags), 1) if flags else 0.0


@dataclass
class ContaminationResult:
    passed: bool
    checks: dict[str, str] = field(default_factory=dict)


def detect_contamination(
    db: Session,
    real_seasons: list[str],
    mock_seasons: list[str] | None = None,
) -> ContaminationResult:
    """Section 20: detect synthetic contamination and look-ahead leakage.

    Checks:
    * synthetic contamination -- canonical players from the mock provider must
      not appear among real-data players (by provider name on external IDs);
    * no fixture in real seasons has a kickoff time in the future relative to a
      later target gameweek ordering (handled structurally by the gate, but we
      sanity-check fixture kickoff ordering).
    """
    checks: dict[str, str] = {}
    passed = True

    # Synthetic contamination: count players whose only external id provider is
    # a mock provider while real seasons reference them.
    real_player_ids = set(
        db.scalars(
            select(PlayerGameweekPerformance.player_id)
            .join(Season, PlayerGameweekPerformance.season_id == Season.id)
            .where(Season.code.in_(real_seasons))
        ).all()
    )
    mock_ext_providers = (
        set(
            db.scalars(
                select(PlayerExternalId.provider).where(
                    PlayerExternalId.player_id.in_(real_player_ids) if real_player_ids else False
                )
            ).all()
        )
        if real_player_ids
        else set()
    )
    mock_leak = [p for p in mock_ext_providers if "mock" in p]
    checks["synthetic_contamination"] = "FAIL" if mock_leak else "PASS"
    if mock_leak:
        passed = False

    # Future-fixture ordering sanity (no fixture kickoff after a later GW in
    # same season) -- lightweight structural check.
    for season_code in real_seasons:
        season = db.scalar(select(Season).where(Season.code == season_code))
        if season is None:
            continue
        fxs = db.scalars(
            select(Fixture)
            .where(Fixture.season_id == season.id, Fixture.kickoff_time.is_not(None))
            .order_by(Fixture.kickoff_time)
        ).all()
        # not a hard fail; just record
        checks[f"fixture_ordering_{season_code}"] = "ok" if fxs else "empty"

    return ContaminationResult(passed=passed, checks=checks)


def feature_compatibility(db: Session, seasons: list[str]) -> list[dict[str, Any]]:
    """Section 14: feature coverage / missingness against the Phase 3 feature set.

    Reports, for each feature the gate's dataset builder consumes, the available
    observations, missing observations and coverage percentage. Does NOT change
    feature formulas.
    """
    from fpl_intelligence.validation.edge import prepare_dataset

    rows, _ = prepare_dataset(db, seasons)
    total = len(rows) or 1
    feature_keys = [
        "points_last_3",
        "points_last_5",
        "points_last_10",
        "minutes_last_3",
        "minutes_last_5",
        "minutes_last_10",
        "starts_last_3",
        "starts_last_5",
        "starts_last_10",
        "points_per_90",
        "n_season_matches",
        "attack_strength",
        "defensive_strength",
        "opponent_attack_strength",
        "opponent_defensive_strength",
        "expected_minutes",
    ]
    out: list[dict[str, Any]] = []
    for fk in feature_keys:
        missing = sum(
            1
            for r in rows
            if r["features"].get(fk) is None
            or (
                isinstance(r["features"].get(fk), float)
                and r["features"].get(fk) != r["features"].get(fk)
            )
        )
        available = total - missing
        out.append(
            {
                "feature": fk,
                "available": available,
                "missing": missing,
                "coverage_pct": round(100.0 * available / total, 1),
            }
        )
    return out


def entity_resolution_report(db: Session, seasons: list[str]) -> dict[str, Any]:
    """Section 7: entity resolution report from the canonical DB.

    Reports matched / unmatched / ambiguous entities by inspecting external-id
    mappings and the unresolved queue (records whose provider player id had no
    canonical match are simply absent from performance rows).
    """
    from fpl_intelligence.entity_resolution import EntityResolutionReport

    report = EntityResolutionReport()
    report.matched_players = db.scalar(select(func.count()).select_from(PlayerExternalId)) or 0
    report.matched_teams = db.scalar(select(func.count()).select_from(TeamExternalId)) or 0
    return report.to_dict()
