"""Mock historical data provider for testing and development.

Provides realistic mock data for multiple seasons and simulates
different provider schemas for testing the normalization layer.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta


def _team_id(prefix: str, season_index: int, team_index: int) -> str:
    return f"{prefix}_team_{season_index}_{team_index}"


def _player_id(prefix: str, season_index: int, player_index: int) -> str:
    return f"{prefix}_player_{season_index}_{player_index}"


def _fixture_id(prefix: str, season_index: int, fixture_index: int) -> str:
    return f"{prefix}_fixture_{season_index}_{fixture_index}"


SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26", "2026-27"]

TEAM_NAMES = [
    "Arsenal",
    "Aston Villa",
    "Bournemouth",
    "Brentford",
    "Brighton",
    "Chelsea",
    "Crystal Palace",
    "Everton",
    "Fulham",
    "Liverpool",
    "Manchester City",
    "Manchester United",
    "Newcastle",
    "Nottingham Forest",
    "Southampton",
    "Tottenham",
    "West Ham",
    "Wolves",
]

PLAYER_FIRST_NAMES = [
    "Mohamed",
    "Kevin",
    "Harry",
    "Erling",
    "Bukayo",
    "Martin",
    "Bruno",
    "Son",
    "James",
    "Phil",
    "Marcus",
    "Gabriel",
    "William",
    "Jack",
    "Declan",
    "Trent",
    "Andrew",
    "Kieran",
    "Ben",
    "Aaron",
    "Jordan",
    "John",
    "David",
    "Thomas",
    "Kai",
    "Cole",
    "Michael",
    "Chris",
    "Daniel",
    "Ryan",
]

PLAYER_SECOND_NAMES = [
    "Salah",
    "De Bruyne",
    "Kane",
    "Haaland",
    "Saka",
    "Odegaard",
    "Fernandes",
    "Heung-min",
    "Maddison",
    "Foden",
    "Rashford",
    "Jesus",
    "Saliba",
    "Grealish",
    "Rice",
    "Alexander-Arnold",
    "Robertson",
    "Tierney",
    "White",
    "Ramsdale",
    "Pickford",
    "Stones",
    "Silva",
    "Partey",
    "Havertz",
    "Palmer",
    "Wood",
    "Wilson",
    "James",
    "Fraser",
]

POSITIONS = [1, 2, 3, 4]  # GKP, DEF, MID, FWD


class MockHistoricalDataProvider:
    """Mock historical data provider for testing.

    Generates realistic synthetic data for multiple seasons.
    Supports two different 'schema' modes to test normalization.
    """

    def __init__(self, provider_name: str = "mock_provider", schema_version: str = "v1") -> None:
        self._provider_name = provider_name
        self.schema_version = schema_version
        self._season_data: dict[str, dict] = {}
        self._generate_data()

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def _generate_data(self) -> None:
        for season_index, season_name in enumerate(SEASONS):
            teams = []
            for team_index, name in enumerate(TEAM_NAMES):
                teams.append(
                    {
                        "provider_team_id": _team_id(self._provider_name, season_index, team_index),
                        "name": name,
                        "short_name": name[:3].upper(),
                    }
                )

            players = []
            for player_index in range(30):
                team_index = player_index % len(TEAM_NAMES)
                first = PLAYER_FIRST_NAMES[player_index % len(PLAYER_FIRST_NAMES)]
                second = PLAYER_SECOND_NAMES[player_index % len(PLAYER_SECOND_NAMES)]
                players.append(
                    {
                        "provider_player_id": _player_id(
                            self._provider_name, season_index, player_index
                        ),
                        "first_name": first,
                        "second_name": second,
                        "web_name": f"{first[0]}. {second}",
                        "position_code": POSITIONS[player_index % len(POSITIONS)],
                        "team_id": _team_id(self._provider_name, season_index, team_index),
                    }
                )

            fixtures = []
            fixture_index = 0
            for gw in range(1, 39):
                for _match in range(0, len(TEAM_NAMES) // 2):
                    home_idx = (fixture_index * 2) % len(TEAM_NAMES)
                    away_idx = (fixture_index * 2 + 1) % len(TEAM_NAMES)
                    kickoff = datetime(2022 + season_index, 8, 1, 15, 0, tzinfo=UTC) + timedelta(
                        days=fixture_index * 7
                    )
                    fixtures.append(
                        {
                            "provider_fixture_id": _fixture_id(
                                self._provider_name, season_index, fixture_index
                            ),
                            "gameweek": gw,
                            "kickoff_time": kickoff.isoformat(),
                            "home_team_id": _team_id(self._provider_name, season_index, home_idx),
                            "away_team_id": _team_id(self._provider_name, season_index, away_idx),
                            "home_score": fixture_index % 5,
                            "away_score": (fixture_index * 2) % 4,
                            "status": "completed",
                            "postponed": False,
                        }
                    )
                    fixture_index += 1

            # Add one postponed fixture
            fixtures.append(
                {
                    "provider_fixture_id": _fixture_id(
                        self._provider_name, season_index, fixture_index
                    ),
                    "gameweek": 1,
                    "kickoff_time": None,
                    "home_team_id": _team_id(self._provider_name, season_index, 0),
                    "away_team_id": _team_id(self._provider_name, season_index, 1),
                    "home_score": None,
                    "away_score": None,
                    "status": "postponed",
                    "postponed": True,
                }
            )

            player_match_stats = []
            for p_idx, player in enumerate(players):
                team_id = player["team_id"]
                for f_idx, fixture in enumerate(fixtures[:5]):  # Only first 5 fixtures
                    if fixture["home_team_id"] == team_id or fixture["away_team_id"] == team_id:
                        player_match_stats.append(
                            {
                                "provider_player_id": player["provider_player_id"],
                                "provider_fixture_id": fixture["provider_fixture_id"],
                                "team_id": team_id,
                                "minutes": 90 if f_idx % 2 == 0 else 0,
                                "goals_scored": 1 if f_idx == 0 else 0,
                                "assists": 1 if f_idx == 1 else 0,
                                "clean_sheets": 1 if f_idx % 2 == 0 else 0,
                                "goals_conceded": 0 if f_idx % 2 == 0 else 2,
                                "own_goals": 0,
                                "penalties_saved": 0,
                                "penalties_missed": 0,
                                "yellow_cards": 0 if f_idx % 3 == 0 else 1,
                                "red_cards": 0,
                                "saves": 3 if POSITIONS[p_idx % len(POSITIONS)] == 1 else 0,
                                "bonus": 3 if f_idx == 0 else 0,
                                "bps": 30 - f_idx * 3,
                                "influence": 50.0 - f_idx * 5.0,
                                "creativity": 30.0 - f_idx * 3.0,
                                "threat": 40.0 - f_idx * 4.0,
                                "ict_index": 12.0 - f_idx * 1.2,
                                "expected_goals": 0.5 if f_idx == 0 else 0.0,
                                "expected_assists": 0.3 if f_idx == 1 else 0.0,
                                "total_points": 10 - f_idx * 2,
                                "was_home": fixture["home_team_id"] == team_id,
                            }
                        )

            fpl_history = []
            for p_idx, player in enumerate(players):
                for gw in range(1, 39):
                    fpl_history.append(
                        {
                            "provider_player_id": player["provider_player_id"],
                            "season_name": season_name,
                            "gameweek": gw,
                            "total_points": max(0, gw % 10 - p_idx % 3),
                            "minutes": 90 if (gw + p_idx) % 2 == 0 else 0,
                            "goals_scored": 1 if gw % 5 == 0 else 0,
                            "assists": 1 if gw % 7 == 0 else 0,
                            "clean_sheets": 1 if gw % 3 == 0 else 0,
                            "goals_conceded": 0 if gw % 3 == 0 else 2,
                            "own_goals": 0,
                            "penalties_saved": 0,
                            "penalties_missed": 0,
                            "yellow_cards": 0 if gw % 4 == 0 else 1,
                            "red_cards": 0,
                            "saves": 3
                            if POSITIONS[p_idx % len(POSITIONS)] == 1 and gw % 2 == 0
                            else 0,
                            "bonus": 3 if gw % 10 == 0 else 0,
                            "bps": 25 - p_idx % 10,
                            "influence": 30.0 - (p_idx % 10) * 2.0,
                            "creativity": 20.0 - (p_idx % 10) * 1.5,
                            "threat": 25.0 - (p_idx % 10) * 2.0,
                            "ict_index": 7.5 - (p_idx % 10) * 0.5,
                            "expected_goals": 0.3 if gw % 5 == 0 else 0.0,
                            "expected_assists": 0.2 if gw % 7 == 0 else 0.0,
                            "expected_goal_involvements": 0.5
                            if (gw % 5 == 0 or gw % 7 == 0)
                            else 0.0,
                            "expected_goals_conceded": 1.5 if gw % 3 != 0 else 0.5,
                            "value": 100 - p_idx * 2,
                            "transfers_balance": 0,
                            "selected": 500000 - p_idx * 10000,
                            "transfers_in": 10000 - p_idx * 300,
                            "transfers_out": 5000 - p_idx * 100,
                            "price": 8.0 - p_idx * 0.2,
                            "selected_by_percent": 20.0 - p_idx * 0.5,
                            "form": 3.0 - p_idx * 0.1,
                            "points_per_game": 4.0 - p_idx * 0.1,
                            "ep_this": 3.0,
                            "ep_next": 3.5,
                        }
                    )

            fpl_snapshots = []
            for p_idx, player in enumerate(players):
                for gw in range(1, 39):
                    snap_time = datetime(2022 + season_index, 8, 1, 18, 0, tzinfo=UTC) + timedelta(
                        days=(gw - 1) * 7
                    )
                    fpl_snapshots.append(
                        {
                            "provider_player_id": player["provider_player_id"],
                            "gameweek": gw,
                            "event_time": snap_time.isoformat(),
                            "published_at": snap_time.isoformat(),
                            "price": 8.0 - p_idx * 0.2 + gw * 0.01,
                            "selected_by_percent": 20.0 - p_idx * 0.5 - gw * 0.1,
                            "transfers_in_event": 10000 - p_idx * 300 - gw * 50,
                            "transfers_out_event": 5000 - p_idx * 100 + gw * 30,
                            "total_points": gw * 4 - p_idx,
                            "form": 3.0 - p_idx * 0.1 + gw * 0.02,
                            "points_per_game": 4.0 - p_idx * 0.1,
                        }
                    )

            self._season_data[season_name] = {
                "teams": teams,
                "players": players,
                "fixtures": fixtures,
                "player_match_stats": player_match_stats,
                "fpl_history": fpl_history,
                "fpl_snapshots": fpl_snapshots,
            }

    def get_seasons(self) -> Sequence[Mapping[str, object]]:
        result = []
        for season_index, season_name in enumerate(SEASONS):
            result.append(
                {
                    "season_name": season_name,
                    "start_date": datetime(2022 + season_index, 8, 1, tzinfo=UTC),
                    "end_date": datetime(2023 + season_index, 5, 31, tzinfo=UTC),
                    "competition": "Premier League",
                }
            )
        return result

    def get_teams(self, season: str) -> Sequence[Mapping[str, object]]:
        data = self._season_data.get(season)
        if data is None:
            return []
        if self.schema_version == "v2":
            # Simulate a different provider schema
            return [
                {
                    "team_id": t["provider_team_id"],
                    "full_name": t["name"],
                    "abbreviation": t["short_name"],
                }
                for t in data["teams"]
            ]
        return data["teams"]

    def get_players(self, season: str) -> Sequence[Mapping[str, object]]:
        data = self._season_data.get(season)
        if data is None:
            return []
        if self.schema_version == "v2":
            # Simulate a different provider schema with different field names
            return [
                {
                    "id": p["provider_player_id"],
                    "given_name": p["first_name"],
                    "family_name": p["second_name"],
                    "display_name": p["web_name"],
                    "position": p["position_code"],
                    "current_team": p["team_id"],
                }
                for p in data["players"]
            ]
        return data["players"]

    def get_fixtures(self, season: str) -> Sequence[Mapping[str, object]]:
        data = self._season_data.get(season)
        if data is None:
            return []
        if self.schema_version == "v2":
            return [
                {
                    "match_id": f["provider_fixture_id"],
                    "round": f["gameweek"],
                    "date": f["kickoff_time"],
                    "home": f["home_team_id"],
                    "away": f["away_team_id"],
                    "home_score": f["home_score"],
                    "away_score": f["away_score"],
                    "match_status": f["status"],
                    "is_postponed": f["postponed"],
                }
                for f in data["fixtures"]
            ]
        return data["fixtures"]

    def get_player_match_stats(self, season: str, player_id: str) -> Sequence[Mapping[str, object]]:
        data = self._season_data.get(season)
        if data is None:
            return []
        return [s for s in data["player_match_stats"] if s["provider_player_id"] == player_id]

    def get_team_match_stats(self, season: str, team_id: str) -> Sequence[Mapping[str, object]]:
        return []

    def get_fpl_history(self, season: str) -> Sequence[Mapping[str, object]]:
        data = self._season_data.get(season)
        if data is None:
            return []
        return data["fpl_history"]

    def get_fpl_snapshots(
        self, season: str, gameweek: int | None = None
    ) -> Sequence[Mapping[str, object]]:
        data = self._season_data.get(season)
        if data is None:
            return []
        snapshots = data["fpl_snapshots"]
        if gameweek is not None:
            return [s for s in snapshots if s["gameweek"] == gameweek]
        return snapshots
