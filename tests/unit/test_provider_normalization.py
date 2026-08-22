"""Tests for provider normalization.

Tests that at least two different mock provider schemas can be normalized
into the same canonical model.
"""

from datetime import datetime

from fpl_intelligence.domain.canonical import (
    normalize_fixture,
    normalize_fpl_history,
    normalize_player,
    normalize_season,
    normalize_team,
)


class TestProviderNormalization:
    """Test that different provider schemas normalize to the same format."""

    def test_v1_team_normalization(self) -> None:
        data = {
            "provider_team_id": "v1_team_1",
            "name": "Arsenal",
            "short_name": "ARS",
        }
        result = normalize_team(data, "provider_v1", schema_version="v1")
        assert result["provider_team_id"] == "v1_team_1"
        assert result["name"] == "Arsenal"
        assert result["short_name"] == "ARS"

    def test_v2_team_normalization(self) -> None:
        """v2 uses different field names but normalizes to the same format."""
        data = {
            "team_id": "v2_team_1",
            "full_name": "Arsenal",
            "abbreviation": "ARS",
        }
        result = normalize_team(data, "provider_v2", schema_version="v2")
        assert result["provider_team_id"] == "v2_team_1"
        assert result["name"] == "Arsenal"
        assert result["short_name"] == "ARS"

    def test_v1_and_v2_team_normalization_identical_output(self) -> None:
        """Both v1 and v2 should produce the same canonical format."""
        v1_data = {
            "provider_team_id": "team_1",
            "name": "Liverpool",
            "short_name": "LIV",
        }
        v2_data = {
            "team_id": "team_1",
            "full_name": "Liverpool",
            "abbreviation": "LIV",
        }
        v1_result = normalize_team(v1_data, "v1", schema_version="v1")
        v2_result = normalize_team(v2_data, "v2", schema_version="v2")
        assert v1_result["provider_team_id"] == v2_result["provider_team_id"]
        assert v1_result["name"] == v2_result["name"]
        assert v1_result["short_name"] == v2_result["short_name"]

    def test_v1_player_normalization(self) -> None:
        data = {
            "provider_player_id": "v1_player_1",
            "first_name": "Mohamed",
            "second_name": "Salah",
            "web_name": "M. Salah",
            "position_code": 3,
            "team_id": "v1_team_1",
        }
        result = normalize_player(data, "provider_v1", schema_version="v1")
        assert result["provider_player_id"] == "v1_player_1"
        assert result["first_name"] == "Mohamed"
        assert result["second_name"] == "Salah"
        assert result["position_code"] == 3

    def test_v2_player_normalization(self) -> None:
        """v2 uses different field names."""
        data = {
            "id": "v2_player_1",
            "given_name": "Mohamed",
            "family_name": "Salah",
            "display_name": "M. Salah",
            "position": 3,
            "current_team": "v2_team_1",
        }
        result = normalize_player(data, "provider_v2", schema_version="v2")
        assert result["provider_player_id"] == "v2_player_1"
        assert result["first_name"] == "Mohamed"
        assert result["second_name"] == "Salah"
        assert result["position_code"] == 3

    def test_v1_and_v2_player_normalization_identical_output(self) -> None:
        """Both v1 and v2 should produce the same canonical player format."""
        v1_data = {
            "provider_player_id": "player_1",
            "first_name": "Kevin",
            "second_name": "De Bruyne",
            "web_name": "K. De Bruyne",
            "position_code": 3,
            "team_id": "team_1",
        }
        v2_data = {
            "id": "player_1",
            "given_name": "Kevin",
            "family_name": "De Bruyne",
            "display_name": "K. De Bruyne",
            "position": 3,
            "current_team": "team_1",
        }
        v1_result = normalize_player(v1_data, "v1", schema_version="v1")
        v2_result = normalize_player(v2_data, "v2", schema_version="v2")
        assert v1_result["provider_player_id"] == v2_result["provider_player_id"]
        assert v1_result["first_name"] == v2_result["first_name"]
        assert v1_result["second_name"] == v2_result["second_name"]

    def test_v1_fixture_normalization(self) -> None:
        data = {
            "provider_fixture_id": "v1_fixture_1",
            "gameweek": 1,
            "kickoff_time": "2024-08-16T20:00:00+00:00",
            "home_team_id": "v1_team_1",
            "away_team_id": "v1_team_2",
            "home_score": 2,
            "away_score": 1,
            "status": "completed",
            "postponed": False,
        }
        result = normalize_fixture(data, "provider_v1", schema_version="v1")
        assert result["provider_fixture_id"] == "v1_fixture_1"
        assert result["gameweek"] == 1
        assert result["home_score"] == 2
        assert result["away_score"] == 1
        assert result["status"] == "completed"
        assert result["postponed"] is False

    def test_v2_fixture_normalization(self) -> None:
        data = {
            "match_id": "v2_fixture_1",
            "round": 1,
            "date": "2024-08-16T20:00:00+00:00",
            "home": "v2_team_1",
            "away": "v2_team_2",
            "home_score": 2,
            "away_score": 1,
            "match_status": "completed",
            "is_postponed": False,
        }
        result = normalize_fixture(data, "provider_v2", schema_version="v2")
        assert result["provider_fixture_id"] == "v2_fixture_1"
        assert result["gameweek"] == 1
        assert result["home_score"] == 2
        assert result["away_score"] == 1
        assert result["status"] == "completed"
        assert result["postponed"] is False

    def test_season_normalization(self) -> None:
        data = {
            "season_name": "2024-25",
            "start_date": datetime(2024, 8, 1),
            "end_date": datetime(2025, 5, 31),
            "competition": "Premier League",
        }
        result = normalize_season(data, "test_provider")
        assert result["code"] == "2024-25"
        assert result["display_name"] == "2024/25"
        assert result["competition"] == "Premier League"

    def test_fpl_history_normalization(self) -> None:
        data = {
            "provider_player_id": "player_1",
            "season_name": "2024-25",
            "gameweek": 1,
            "total_points": 10,
            "minutes": 90,
            "goals_scored": 1,
            "assists": 0,
            "price": 8.5,
            "selected_by_percent": 15.0,
            "form": 5.0,
        }
        result = normalize_fpl_history(data, "test_provider")
        assert result["provider_player_id"] == "player_1"
        assert result["gameweek"] == 1
        assert result["total_points"] == 10
        assert result["minutes"] == 90
        assert result["price"] == 8.5
