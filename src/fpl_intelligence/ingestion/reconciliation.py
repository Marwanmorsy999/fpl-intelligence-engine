"""Data reconciliation engine.

Detects and reports data quality issues during historical data ingestion.
The reconciliation report provides a detailed account of what was accepted,
rejected, and what anomalies were found.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReconciliationIssue:
    """A single issue found during reconciliation."""
    severity: str  # "critical", "warning", "info"
    category: str  # e.g. "missing_player", "duplicate_fixture", "invalid_minutes"
    message: str
    details: dict[str, Any] | None = None


@dataclass
class ReconciliationReport:
    """Report of reconciliation results for a data import."""
    season: str
    provider: str
    dataset: str

    records_received: int = 0
    records_accepted: int = 0
    records_rejected: int = 0

    unmatched_teams: list[str] = field(default_factory=list)
    unmatched_players: list[str] = field(default_factory=list)
    duplicate_candidates: list[dict[str, Any]] = field(default_factory=list)
    missing_fixtures: list[str] = field(default_factory=list)
    duplicate_fixtures: list[str] = field(default_factory=list)

    warnings: list[ReconciliationIssue] = field(default_factory=list)
    critical_errors: list[ReconciliationIssue] = field(default_factory=list)

    def has_critical_errors(self) -> bool:
        return len(self.critical_errors) > 0

    def add_critical(self, category: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.critical_errors.append(
            ReconciliationIssue(severity="critical", category=category, message=message, details=details)
        )

    def add_warning(self, category: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.warnings.append(
            ReconciliationIssue(severity="warning", category=category, message=message, details=details)
        )

    def summary(self) -> str:
        lines = [
            f"Reconciliation Report: {self.provider}/{self.dataset} ({self.season})",
            f"  Received: {self.records_received}",
            f"  Accepted: {self.records_accepted}",
            f"  Rejected: {self.records_rejected}",
            f"  Unmatched teams: {len(self.unmatched_teams)}",
            f"  Unmatched players: {len(self.unmatched_players)}",
            f"  Duplicate candidates: {len(self.duplicate_candidates)}",
            f"  Warnings: {len(self.warnings)}",
            f"  Critical errors: {len(self.critical_errors)}",
        ]
        if self.critical_errors:
            lines.append("  Critical errors:")
            for err in self.critical_errors:
                lines.append(f"    - [{err.category}] {err.message}")
        if self.warnings:
            lines.append("  Warnings:")
            for warn in self.warnings:
                lines.append(f"    - [{warn.category}] {warn.message}")
        return "\n".join(lines)


def reconcile_teams(
    provider_teams: Sequence[dict[str, Any]],
    known_team_ids: set[str],
    report: ReconciliationReport,
) -> dict[str, dict[str, Any]]:
    """Reconcile provider teams against known teams.

    Returns a mapping of provider_team_id -> team data for accepted teams.
    """
    accepted: dict[str, dict[str, Any]] = {}
    report.records_received += len(provider_teams)

    for team in provider_teams:
        provider_id = team.get("provider_team_id", "")
        if not provider_id:
            report.add_critical("missing_team_id", f"Team missing provider_team_id: {team}")
            report.records_rejected += 1
            continue

        if provider_id in known_team_ids:
            accepted[provider_id] = team
            report.records_accepted += 1
        else:
            report.unmatched_teams.append(provider_id)
            report.add_warning("unmatched_team", f"Team {provider_id} ({team.get('name')}) not found in known teams")
            report.records_rejected += 1

    return accepted


def reconcile_players(
    provider_players: Sequence[dict[str, Any]],
    known_player_ids: set[str],
    report: ReconciliationReport,
) -> dict[str, dict[str, Any]]:
    """Reconcile provider players against known players.

    Returns a mapping of provider_player_id -> player data for accepted players.
    """
    accepted: dict[str, dict[str, Any]] = {}
    report.records_received += len(provider_players)

    for player in provider_players:
        provider_id = player.get("provider_player_id", "")
        if not provider_id:
            report.add_critical("missing_player_id", f"Player missing provider_player_id: {player}")
            report.records_rejected += 1
            continue

        if provider_id in known_player_ids:
            accepted[provider_id] = player
            report.records_accepted += 1
        else:
            report.unmatched_players.append(provider_id)
            report.add_warning("unmatched_player", f"Player {provider_id} ({player.get('web_name')}) not found in known players")
            report.records_rejected += 1

    return accepted


def reconcile_fixtures(
    provider_fixtures: Sequence[dict[str, Any]],
    known_fixture_ids: set[str],
    known_team_ids: set[str],
    report: ReconciliationReport,
) -> list[dict[str, Any]]:
    """Reconcile provider fixtures against known fixtures and teams.

    Returns a list of accepted fixture data.
    """
    accepted: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    report.records_received += len(provider_fixtures)

    for fixture in provider_fixtures:
        provider_id = fixture.get("provider_fixture_id", "")
        if not provider_id:
            report.add_critical("missing_fixture_id", f"Fixture missing provider_fixture_id: {fixture}")
            report.records_rejected += 1
            continue

        # Check for duplicate fixture IDs
        if provider_id in seen_ids:
            report.duplicate_fixtures.append(provider_id)
            report.add_warning("duplicate_fixture", f"Duplicate fixture ID: {provider_id}")
            report.records_rejected += 1
            continue
        seen_ids.add(provider_id)

        # Check if fixture references valid teams
        home_team = fixture.get("home_team_id", "")
        away_team = fixture.get("away_team_id", "")
        if home_team not in known_team_ids:
            report.add_critical("invalid_home_team", f"Fixture {provider_id} references unknown home team: {home_team}")
            report.records_rejected += 1
            continue
        if away_team not in known_team_ids:
            report.add_critical("invalid_away_team", f"Fixture {provider_id} references unknown away team: {away_team}")
            report.records_rejected += 1
            continue

        # Check for inconsistent scores
        home_score = fixture.get("home_score")
        away_score = fixture.get("away_score")
        if home_score is not None and away_score is not None:
            if home_score < 0 or away_score < 0:
                report.add_warning("negative_score", f"Fixture {provider_id} has negative score: {home_score}-{away_score}")

        accepted.append(fixture)
        report.records_accepted += 1

    return accepted


def validate_player_match_stats(
    stats: Sequence[dict[str, Any]],
    known_player_ids: set[str],
    known_fixture_ids: set[str],
    report: ReconciliationReport,
) -> list[dict[str, Any]]:
    """Validate player match statistics.

    Returns a list of accepted player match stats.
    """
    accepted: list[dict[str, Any]] = []
    report.records_received += len(stats)

    for stat in stats:
        player_id = stat.get("provider_player_id", "")
        fixture_id = stat.get("provider_fixture_id", "")

        if not player_id:
            report.add_critical("missing_player_id", "Player match stat missing provider_player_id")
            report.records_rejected += 1
            continue

        if not fixture_id:
            report.add_critical("missing_fixture_id", "Player match stat missing provider_fixture_id")
            report.records_rejected += 1
            continue

        if player_id not in known_player_ids:
            report.add_warning("unmatched_player", f"Player match stat references unknown player: {player_id}")
            report.records_rejected += 1
            continue

        if fixture_id not in known_fixture_ids:
            report.add_warning("unmatched_fixture", f"Player match stat references unknown fixture: {fixture_id}")
            report.records_rejected += 1
            continue

        # Validate minutes
        minutes = stat.get("minutes")
        if minutes is not None and (minutes < 0 or minutes > 120):
            report.add_warning("impossible_minutes", f"Player {player_id} fixture {fixture_id}: impossible minutes {minutes}")
            report.records_rejected += 1
            continue

        accepted.append(stat)
        report.records_accepted += 1

    return accepted


def validate_fpl_history(
    history: Sequence[dict[str, Any]],
    known_player_ids: set[str],
    report: ReconciliationReport,
) -> list[dict[str, Any]]:
    """Validate FPL history data.

    Returns a list of accepted FPL history records.
    """
    accepted: list[dict[str, Any]] = []
    report.records_received += len(history)

    for record in history:
        player_id = record.get("provider_player_id", "")
        if not player_id:
            report.add_critical("missing_player_id", "FPL history record missing provider_player_id")
            report.records_rejected += 1
            continue

        if player_id not in known_player_ids:
            report.add_warning("unmatched_player", f"FPL history references unknown player: {player_id}")
            report.records_rejected += 1
            continue

        # Validate gameweek
        gameweek = record.get("gameweek")
        if gameweek is not None and (gameweek < 1 or gameweek > 38):
            report.add_warning("invalid_gameweek", f"FPL history for player {player_id}: invalid gameweek {gameweek}")

        # Validate minutes
        minutes = record.get("minutes")
        if minutes is not None and (minutes < 0 or minutes > 120):
            report.add_warning("impossible_minutes", f"FPL history for player {player_id}: impossible minutes {minutes}")

        accepted.append(record)
        report.records_accepted += 1

    return accepted