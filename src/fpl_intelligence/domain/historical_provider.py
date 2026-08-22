"""Historical football data provider abstraction.

Defines the protocol for any external data source that provides historical
football/FPL data. Different providers may supply different subsets of data.
The normalization layer is responsible for combining multiple providers into
a single canonical database.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol


class HistoricalSeasonData(Mapping[str, object]):
    """Represents a season from a provider's perspective."""

    season_name: str
    start_date: datetime | None
    end_date: datetime | None
    competition: str | None


class HistoricalTeamData(Mapping[str, object]):
    """Represents a team from a provider's perspective."""

    provider_team_id: str
    name: str
    short_name: str | None


class HistoricalPlayerData(Mapping[str, object]):
    """Represents a player from a provider's perspective."""

    provider_player_id: str
    first_name: str
    second_name: str
    web_name: str
    position_code: int | None


class HistoricalFixtureData(Mapping[str, object]):
    """Represents a fixture from a provider's perspective."""

    provider_fixture_id: str
    gameweek: int | None
    kickoff_time: datetime | None
    home_team_id: str
    away_team_id: str
    home_score: int | None
    away_score: int | None
    status: str | None
    postponed: bool | None


class HistoricalPlayerMatchData(Mapping[str, object]):
    """Represents a player's match statistics from a provider's perspective."""

    provider_player_id: str
    provider_fixture_id: str
    team_id: str | None
    minutes: int | None
    goals_scored: int | None
    assists: int | None
    clean_sheets: int | None
    goals_conceded: int | None
    own_goals: int | None
    penalties_saved: int | None
    penalties_missed: int | None
    yellow_cards: int | None
    red_cards: int | None
    saves: int | None
    bonus: int | None
    bps: int | None
    influence: float | None
    creativity: float | None
    threat: float | None
    ict_index: float | None
    expected_goals: float | None
    expected_assists: float | None
    total_points: int | None
    was_home: bool | None


class HistoricalTeamMatchData(Mapping[str, object]):
    """Represents a team's match statistics from a provider's perspective."""

    provider_team_id: str
    provider_fixture_id: str
    is_home: bool
    goals_scored: int | None
    goals_conceded: int | None
    expected_goals: float | None
    expected_goals_conceded: float | None
    shots: int | None
    shots_on_target: int | None
    possession: float | None


class HistoricalFPLHistoryData(Mapping[str, object]):
    """Represents a player's FPL history data from a provider's perspective."""

    provider_player_id: str
    season_name: str
    gameweek: int | None
    total_points: int | None
    minutes: int | None
    goals_scored: int | None
    assists: int | None
    clean_sheets: int | None
    goals_conceded: int | None
    own_goals: int | None
    penalties_saved: int | None
    penalties_missed: int | None
    yellow_cards: int | None
    red_cards: int | None
    saves: int | None
    bonus: int | None
    bps: int | None
    influence: float | None
    creativity: float | None
    threat: float | None
    ict_index: float | None
    expected_goals: float | None
    expected_assists: float | None
    expected_goal_involvements: float | None
    expected_goals_conceded: float | None
    value: int | None
    transfers_balance: int | None
    selected: int | None
    transfers_in: int | None
    transfers_out: int | None
    price: float | None
    selected_by_percent: float | None
    form: float | None
    points_per_game: float | None
    ep_this: float | None
    ep_next: float | None


class HistoricalFootballDataProvider(Protocol):
    """Protocol for historical football/FPL data providers.

    Different providers may implement different subsets of these methods.
    Each method documents the expected data shape for the normalization layer.
    """

    @property
    def provider_name(self) -> str:
        """Unique name for this provider, e.g. 'official_fpl', 'understat', 'fbref'."""
        ...

    def get_seasons(self) -> Sequence[Mapping[str, object]]:
        """Return list of available seasons."""
        ...

    def get_teams(self, season: str) -> Sequence[Mapping[str, object]]:
        """Return list of teams for a given season."""
        ...

    def get_players(self, season: str) -> Sequence[Mapping[str, object]]:
        """Return list of players for a given season."""
        ...

    def get_fixtures(self, season: str) -> Sequence[Mapping[str, object]]:
        """Return list of fixtures for a given season."""
        ...

    def get_player_match_stats(self, season: str, player_id: str) -> Sequence[Mapping[str, object]]:
        """Return match-level statistics for a specific player in a season."""
        ...

    def get_team_match_stats(self, season: str, team_id: str) -> Sequence[Mapping[str, object]]:
        """Return match-level team statistics for a specific team in a season."""
        ...

    def get_fpl_history(self, season: str) -> Sequence[Mapping[str, object]]:
        """Return FPL history data for all players in a season."""
        ...

    def get_fpl_snapshots(
        self, season: str, gameweek: int | None = None
    ) -> Sequence[Mapping[str, object]]:
        """Return FPL snapshot data (price, ownership, etc.) at a point in time."""
        ...
