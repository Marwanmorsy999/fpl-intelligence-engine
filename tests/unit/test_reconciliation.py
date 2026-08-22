"""Tests for the reconciliation engine."""

from fpl_intelligence.ingestion.reconciliation import (
    ReconciliationReport,
    reconcile_fixtures,
    reconcile_teams,
    validate_fpl_history,
    validate_player_match_stats,
)


class TestReconciliation:
    """Test the reconciliation engine."""

    def test_empty_report(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="all")
        assert not report.has_critical_errors()
        assert len(report.critical_errors) == 0
        assert len(report.warnings) == 0

    def test_report_with_errors(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="all")
        report.add_critical("missing_data", "No data found")
        assert report.has_critical_errors()
        summary = report.summary()
        assert "No data found" in summary

    def test_report_with_warnings(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="all")
        report.add_warning("duplicate", "Duplicate record found")
        assert not report.has_critical_errors()
        summary = report.summary()
        assert "Duplicate record found" in summary

    def test_reconcile_teams_all_known(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="teams")
        teams = [
            {"provider_team_id": "team_1", "name": "Arsenal"},
            {"provider_team_id": "team_2", "name": "Chelsea"},
        ]
        known = {"team_1", "team_2"}
        accepted = reconcile_teams(teams, known, report)
        assert len(accepted) == 2
        assert report.records_received == 2
        assert report.records_accepted == 2
        assert report.records_rejected == 0

    def test_reconcile_teams_with_unmatched(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="teams")
        teams = [
            {"provider_team_id": "team_1", "name": "Arsenal"},
            {"provider_team_id": "team_unknown", "name": "Unknown FC"},
        ]
        known = {"team_1"}
        accepted = reconcile_teams(teams, known, report)
        assert len(accepted) == 1
        assert report.records_rejected == 1
        assert len(report.unmatched_teams) == 1

    def test_reconcile_teams_missing_id(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="teams")
        teams = [{"name": "No ID Team"}]
        accepted = reconcile_teams(teams, set(), report)
        assert len(accepted) == 0
        assert report.records_rejected == 1
        assert len(report.critical_errors) == 1

    def test_reconcile_fixtures_duplicate(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="fixtures")
        fixtures = [
            {
                "provider_fixture_id": "fix_1",
                "home_team_id": "team_1",
                "away_team_id": "team_2",
                "home_score": 1,
                "away_score": 0,
            },
            {
                "provider_fixture_id": "fix_1",
                "home_team_id": "team_1",
                "away_team_id": "team_2",
                "home_score": 2,
                "away_score": 1,
            },
        ]
        known_fixtures = {"fix_1"}
        known_teams = {"team_1", "team_2"}
        accepted = reconcile_fixtures(fixtures, known_fixtures, known_teams, report)
        assert len(accepted) == 1
        assert len(report.duplicate_fixtures) == 1

    def test_reconcile_fixtures_invalid_team(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="fixtures")
        fixtures = [
            {
                "provider_fixture_id": "fix_1",
                "home_team_id": "team_unknown",
                "away_team_id": "team_2",
                "home_score": 1,
                "away_score": 0,
            },
        ]
        known_fixtures = {"fix_1"}
        known_teams = {"team_2"}
        accepted = reconcile_fixtures(fixtures, known_fixtures, known_teams, report)
        assert len(accepted) == 0
        assert len(report.critical_errors) >= 1

    def test_validate_player_match_stats(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="stats")
        stats = [
            {
                "provider_player_id": "player_1",
                "provider_fixture_id": "fix_1",
                "minutes": 90,
                "goals_scored": 1,
            },
        ]
        known_players = {"player_1"}
        known_fixtures = {"fix_1"}
        accepted = validate_player_match_stats(stats, known_players, known_fixtures, report)
        assert len(accepted) == 1

    def test_validate_player_match_stats_impossible_minutes(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="stats")
        stats = [
            {
                "provider_player_id": "player_1",
                "provider_fixture_id": "fix_1",
                "minutes": 200,
            },
        ]
        known_players = {"player_1"}
        known_fixtures = {"fix_1"}
        accepted = validate_player_match_stats(stats, known_players, known_fixtures, report)
        assert len(accepted) == 0
        assert len(report.warnings) >= 1

    def test_validate_fpl_history(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="fpl")
        history = [
            {
                "provider_player_id": "player_1",
                "gameweek": 1,
                "minutes": 90,
                "total_points": 10,
            },
        ]
        known_players = {"player_1"}
        accepted = validate_fpl_history(history, known_players, report)
        assert len(accepted) == 1

    def test_validate_fpl_history_invalid_gameweek(self) -> None:
        report = ReconciliationReport(season="2024-25", provider="test", dataset="fpl")
        history = [
            {
                "provider_player_id": "player_1",
                "gameweek": 99,
                "minutes": 90,
            },
        ]
        known_players = {"player_1"}
        accepted = validate_fpl_history(history, known_players, report)
        assert len(accepted) == 1  # Invalid gameweek is a warning, not rejection
        assert len(report.warnings) >= 1
