"""Historical data validation checks.

Validates database integrity after historical data import.
Each check returns a list of issues found.
"""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import (
    Fixture,
    Gameweek,
    Player,
    PlayerGameweekPerformance,
    Season,
    Team,
)


@dataclass
class HistoricalValidationError:
    """A single validation error."""
    table: str
    field: str
    message: str
    record_id: int | None = None


@dataclass
class HistoricalValidationResult:
    """Result of running all validation checks."""
    passed: bool = True
    errors: list[HistoricalValidationError] = field(default_factory=list)

    def add_error(self, table: str, field: str, message: str, record_id: int | None = None) -> None:
        self.passed = False
        self.errors.append(HistoricalValidationError(table=table, field=field, message=message, record_id=record_id))

    def summary(self) -> str:
        if self.passed:
            return "All validation checks passed."
        lines = [f"Validation FAILED: {len(self.errors)} issue(s) found:"]
        for err in self.errors:
            lines.append(f"  [{err.table}.{err.field}] {err.message} (id={err.record_id})")
        return "\n".join(lines)


def validate_season_integrity(db: Session, season_id: int | None = None) -> HistoricalValidationResult:
    """Validate that gameweeks and fixtures reference valid seasons."""
    result = HistoricalValidationResult()

    # Check gameweeks
    query = select(Gameweek)
    if season_id is not None:
        query = query.where(Gameweek.season_id == season_id)
    gameweeks = db.scalars(query).all()

    for gw in gameweeks:
        season = db.get(Season, gw.season_id)
        if season is None:
            result.add_error("gameweeks", "season_id", f"Gameweek {gw.id} references non-existent season {gw.season_id}", gw.id)

    # Check fixtures
    query = select(Fixture)
    if season_id is not None:
        query = query.where(Fixture.season_id == season_id)
    fixtures = db.scalars(query).all()

    for fixture in fixtures:
        season = db.get(Season, fixture.season_id)
        if season is None:
            result.add_error("fixtures", "season_id", f"Fixture {fixture.id} references non-existent season {fixture.season_id}", fixture.id)

        home_team = db.get(Team, fixture.home_team_id)
        if home_team is None:
            result.add_error("fixtures", "home_team_id", f"Fixture {fixture.id} references non-existent home team {fixture.home_team_id}", fixture.id)

        away_team = db.get(Team, fixture.away_team_id)
        if away_team is None:
            result.add_error("fixtures", "away_team_id", f"Fixture {fixture.id} references non-existent away team {fixture.away_team_id}", fixture.id)

    return result


def validate_gameweek_integrity(db: Session, season_id: int | None = None) -> HistoricalValidationResult:
    """Validate gameweek data integrity."""
    result = HistoricalValidationResult()

    query = select(Gameweek)
    if season_id is not None:
        query = query.where(Gameweek.season_id == season_id)
    gameweeks = db.scalars(query).all()

    for gw in gameweeks:
        # Check deadline ordering
        if gw.start_time and gw.deadline_time and gw.start_time < gw.deadline_time:
            result.add_error(
                "gameweeks", "deadline_time",
                f"Gameweek {gw.id}: deadline ({gw.deadline_time}) is after start ({gw.start_time})",
                gw.id,
            )

        if gw.end_time and gw.start_time and gw.end_time < gw.start_time:
            result.add_error(
                "gameweeks", "end_time",
                f"Gameweek {gw.id}: end ({gw.end_time}) is before start ({gw.start_time})",
                gw.id,
            )

    return result


def validate_fixture_integrity(db: Session, season_id: int | None = None) -> HistoricalValidationResult:
    """Validate fixture data integrity."""
    result = HistoricalValidationResult()

    query = select(Fixture)
    if season_id is not None:
        query = query.where(Fixture.season_id == season_id)
    fixtures = db.scalars(query).all()

    for fixture in fixtures:
        # Check for negative scores
        if fixture.home_score is not None and fixture.home_score < 0:
            result.add_error("fixtures", "home_score", f"Fixture {fixture.id}: negative home score {fixture.home_score}", fixture.id)

        if fixture.away_score is not None and fixture.away_score < 0:
            result.add_error("fixtures", "away_score", f"Fixture {fixture.id}: negative away score {fixture.away_score}", fixture.id)

        # Check home != away
        if fixture.home_team_id == fixture.away_team_id:
            result.add_error("fixtures", "home_team_id", f"Fixture {fixture.id}: home team equals away team", fixture.id)

    return result


def validate_player_stats_integrity(db: Session, season_id: int | None = None) -> HistoricalValidationResult:
    """Validate player performance statistics."""
    result = HistoricalValidationResult()

    query = select(PlayerGameweekPerformance)
    if season_id is not None:
        query = query.where(PlayerGameweekPerformance.season_id == season_id)
    performances = db.scalars(query).all()

    for perf in performances:
        # Check for impossible minutes
        if perf.minutes is not None and (perf.minutes < 0 or perf.minutes > 120):
            result.add_error(
                "player_gameweek_performances", "minutes",
                f"Player {perf.player_id} Gameweek {perf.gameweek_id}: impossible minutes {perf.minutes}",
                perf.id,
            )

        # Check for negative totals
        for field_name in ["goals_scored", "assists", "yellow_cards", "red_cards", "own_goals"]:
            value = getattr(perf, field_name, None)
            if value is not None and value < 0:
                result.add_error(
                    "player_gameweek_performances", field_name,
                    f"Player {perf.player_id} Gameweek {perf.gameweek_id}: negative {field_name} ({value})",
                    perf.id,
                )

        # Check player exists
        player = db.get(Player, perf.player_id)
        if player is None:
            result.add_error("player_gameweek_performances", "player_id", f"Player {perf.player_id} not found", perf.id)

        # Check gameweek exists
        gameweek = db.get(Gameweek, perf.gameweek_id)
        if gameweek is None:
            result.add_error("player_gameweek_performances", "gameweek_id", f"Gameweek {perf.gameweek_id} not found", perf.id)

    return result


def validate_no_duplicate_records(db: Session) -> HistoricalValidationResult:
    """Check for duplicate canonical records."""
    result = HistoricalValidationResult()

    # Check for duplicate player-gameweek records
    subquery = (
        select(
            PlayerGameweekPerformance.player_id,
            PlayerGameweekPerformance.gameweek_id,
            func.count().label("cnt"),
        )
        .group_by(PlayerGameweekPerformance.player_id, PlayerGameweekPerformance.gameweek_id)
        .having(func.count() > 1)
    )
    duplicates = db.execute(subquery).all()
    for dup in duplicates:
        result.add_error(
            "player_gameweek_performances", "player_id, gameweek_id",
            f"Duplicate record: player {dup.player_id}, gameweek {dup.gameweek_id}",
        )

    # Check for duplicate external IDs
    from fpl_intelligence.db.models import PlayerExternalId, TeamExternalId

    pe_subquery = (
        select(
            PlayerExternalId.provider,
            PlayerExternalId.provider_player_id,
            func.count().label("cnt"),
        )
        .group_by(PlayerExternalId.provider, PlayerExternalId.provider_player_id)
        .having(func.count() > 1)
    )
    pe_duplicates = db.execute(pe_subquery).all()
    for dup in pe_duplicates:
        result.add_error(
            "player_external_ids", "provider, provider_player_id",
            f"Duplicate external ID: provider={dup.provider}, id={dup.provider_player_id}",
        )

    te_subquery = (
        select(
            TeamExternalId.provider,
            TeamExternalId.provider_team_id,
            func.count().label("cnt"),
        )
        .group_by(TeamExternalId.provider, TeamExternalId.provider_team_id)
        .having(func.count() > 1)
    )
    te_duplicates = db.execute(te_subquery).all()
    for dup in te_duplicates:
        result.add_error(
            "team_external_ids", "provider, provider_team_id",
            f"Duplicate external ID: provider={dup.provider}, id={dup.provider_team_id}",
        )

    return result