"""Canonical normalization layer.

Converts provider-specific data representations into provider-independent
canonical entities. This is the core of the provider abstraction boundary:
external schemas are mapped to internal models here.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any


def normalize_season(data: Mapping[str, object], provider_name: str) -> dict[str, Any]:
    """Convert provider season data to canonical format."""
    season_name = str(data.get("season_name", ""))
    return {
        "code": season_name,
        "display_name": season_name.replace("-", "/"),
        "start_date": _parse_datetime(data.get("start_date")),
        "end_date": _parse_datetime(data.get("end_date")),
        "competition": str(data.get("competition", "Premier League")),
    }


def normalize_team(data: Mapping[str, object], provider_name: str, schema_version: str = "v1") -> dict[str, Any]:
    """Convert provider team data to canonical format.

    Handles different provider schemas (v1, v2, etc.) by mapping
    field names to the canonical representation.
    """
    if schema_version == "v2":
        return {
            "provider_team_id": str(data.get("team_id", "")),
            "name": str(data.get("full_name", "Unknown")),
            "short_name": str(data.get("abbreviation")) or None,
        }

    return {
        "provider_team_id": str(data.get("provider_team_id", "")),
        "name": str(data.get("name", "Unknown")),
        "short_name": str(data.get("short_name")) or None,
    }


def normalize_player(data: Mapping[str, object], provider_name: str, schema_version: str = "v1") -> dict[str, Any]:
    """Convert provider player data to canonical format.

    Handles different provider schemas by mapping field names.
    """
    if schema_version == "v2":
        return {
            "provider_player_id": str(data.get("id", "")),
            "first_name": str(data.get("given_name", "")),
            "second_name": str(data.get("family_name", "")),
            "web_name": str(data.get("display_name", "")),
            "position_code": _int_or_none(data.get("position")),
            "team_id": str(data.get("current_team", "")),
        }

    return {
        "provider_player_id": str(data.get("provider_player_id", "")),
        "first_name": str(data.get("first_name", "")),
        "second_name": str(data.get("second_name", "")),
        "web_name": str(data.get("web_name", "")),
        "position_code": _int_or_none(data.get("position_code")),
        "team_id": str(data.get("team_id", "")),
    }


def normalize_fixture(data: Mapping[str, object], provider_name: str, schema_version: str = "v1") -> dict[str, Any]:
    """Convert provider fixture data to canonical format.

    Handles different provider schemas by mapping field names.
    """
    if schema_version == "v2":
        return {
            "provider_fixture_id": str(data.get("match_id", "")),
            "gameweek": _int_or_none(data.get("round")),
            "kickoff_time": _parse_datetime(data.get("date")),
            "home_team_id": str(data.get("home", "")),
            "away_team_id": str(data.get("away", "")),
            "home_score": _int_or_none(data.get("home_score")),
            "away_score": _int_or_none(data.get("away_score")),
            "status": str(data.get("match_status", "scheduled")) if data.get("match_status") else "scheduled",
            "postponed": bool(data.get("is_postponed", False)),
        }

    return {
        "provider_fixture_id": str(data.get("provider_fixture_id", "")),
        "gameweek": _int_or_none(data.get("gameweek")),
        "kickoff_time": _parse_datetime(data.get("kickoff_time")),
        "home_team_id": str(data.get("home_team_id", "")),
        "away_team_id": str(data.get("away_team_id", "")),
        "home_score": _int_or_none(data.get("home_score")),
        "away_score": _int_or_none(data.get("away_score")),
        "status": str(data.get("status", "scheduled")) if data.get("status") else "scheduled",
        "postponed": bool(data.get("postponed", False)),
    }


def normalize_player_match_stats(data: Mapping[str, object], provider_name: str) -> dict[str, Any]:
    """Convert provider player match statistics to canonical format."""
    return {
        "provider_player_id": str(data.get("provider_player_id", "")),
        "provider_fixture_id": str(data.get("provider_fixture_id", "")),
        "team_id": str(data.get("team_id", "")) if data.get("team_id") else None,
        "minutes": _int_or_none(data.get("minutes")),
        "goals_scored": _int_or_none(data.get("goals_scored")) or 0,
        "assists": _int_or_none(data.get("assists")) or 0,
        "clean_sheets": _int_or_none(data.get("clean_sheets")) or 0,
        "goals_conceded": _int_or_none(data.get("goals_conceded")) or 0,
        "own_goals": _int_or_none(data.get("own_goals")) or 0,
        "penalties_saved": _int_or_none(data.get("penalties_saved")) or 0,
        "penalties_missed": _int_or_none(data.get("penalties_missed")) or 0,
        "yellow_cards": _int_or_none(data.get("yellow_cards")) or 0,
        "red_cards": _int_or_none(data.get("red_cards")) or 0,
        "saves": _int_or_none(data.get("saves")) or 0,
        "bonus": _int_or_none(data.get("bonus")) or 0,
        "bps": _int_or_none(data.get("bps")) or 0,
        "influence": _float_or_none(data.get("influence")),
        "creativity": _float_or_none(data.get("creativity")),
        "threat": _float_or_none(data.get("threat")),
        "ict_index": _float_or_none(data.get("ict_index")),
        "expected_goals": _float_or_none(data.get("expected_goals")),
        "expected_assists": _float_or_none(data.get("expected_assists")),
        "total_points": _int_or_none(data.get("total_points")) or 0,
        "was_home": data.get("was_home") if isinstance(data.get("was_home"), bool) else None,
    }


def normalize_fpl_history(data: Mapping[str, object], provider_name: str) -> dict[str, Any]:
    """Convert provider FPL history data to canonical format."""
    return {
        "provider_player_id": str(data.get("provider_player_id", "")),
        "season_name": str(data.get("season_name", "")),
        "gameweek": _int_or_none(data.get("gameweek")),
        "total_points": _int_or_none(data.get("total_points")) or 0,
        "minutes": _int_or_none(data.get("minutes")) or 0,
        "goals_scored": _int_or_none(data.get("goals_scored")) or 0,
        "assists": _int_or_none(data.get("assists")) or 0,
        "clean_sheets": _int_or_none(data.get("clean_sheets")) or 0,
        "goals_conceded": _int_or_none(data.get("goals_conceded")) or 0,
        "own_goals": _int_or_none(data.get("own_goals")) or 0,
        "penalties_saved": _int_or_none(data.get("penalties_saved")) or 0,
        "penalties_missed": _int_or_none(data.get("penalties_missed")) or 0,
        "yellow_cards": _int_or_none(data.get("yellow_cards")) or 0,
        "red_cards": _int_or_none(data.get("red_cards")) or 0,
        "saves": _int_or_none(data.get("saves")) or 0,
        "bonus": _int_or_none(data.get("bonus")) or 0,
        "bps": _int_or_none(data.get("bps")) or 0,
        "influence": _float_or_none(data.get("influence")),
        "creativity": _float_or_none(data.get("creativity")),
        "threat": _float_or_none(data.get("threat")),
        "ict_index": _float_or_none(data.get("ict_index")),
        "expected_goals": _float_or_none(data.get("expected_goals")),
        "expected_assists": _float_or_none(data.get("expected_assists")),
        "expected_goal_involvements": _float_or_none(data.get("expected_goal_involvements")),
        "expected_goals_conceded": _float_or_none(data.get("expected_goals_conceded")),
        "value": _int_or_none(data.get("value")),
        "transfers_balance": _int_or_none(data.get("transfers_balance")),
        "selected": _int_or_none(data.get("selected")),
        "transfers_in": _int_or_none(data.get("transfers_in")) or 0,
        "transfers_out": _int_or_none(data.get("transfers_out")) or 0,
        "price": _float_or_none(data.get("price")),
        "selected_by_percent": _float_or_none(data.get("selected_by_percent")),
        "form": _float_or_none(data.get("form")),
        "points_per_game": _float_or_none(data.get("points_per_game")),
        "ep_this": _float_or_none(data.get("ep_this")),
        "ep_next": _float_or_none(data.get("ep_next")),
    }


def normalize_fpl_snapshot(data: Mapping[str, object], provider_name: str) -> dict[str, Any]:
    """Convert provider FPL snapshot data to canonical format."""
    event_time = _parse_datetime(data.get("event_time"))
    if event_time is None:
        event_time = datetime.now()

    return {
        "provider_player_id": str(data.get("provider_player_id", "")),
        "gameweek": _int_or_none(data.get("gameweek")),
        "event_time": event_time,
        "published_at": _parse_datetime(data.get("published_at")),
        "price": _float_or_none(data.get("price")),
        "selected_by_percent": _float_or_none(data.get("selected_by_percent")),
        "transfers_in_event": _int_or_none(data.get("transfers_in_event")) or 0,
        "transfers_out_event": _int_or_none(data.get("transfers_out_event")) or 0,
        "transfers_in_season": _int_or_none(data.get("transfers_in_season")) or 0,
        "transfers_out_season": _int_or_none(data.get("transfers_out_season")) or 0,
        "total_points": _int_or_none(data.get("total_points")) or 0,
        "form": _float_or_none(data.get("form")),
        "points_per_game": _float_or_none(data.get("points_per_game")),
        "form_rank": _int_or_none(data.get("form_rank")),
        "points_per_game_rank": _int_or_none(data.get("points_per_game_rank")),
        "selected_rank": _int_or_none(data.get("selected_rank")),
        "ep_this": _float_or_none(data.get("ep_this")),
        "ep_next": _float_or_none(data.get("ep_next")),
    }


def _parse_datetime(value: object) -> datetime | None:
    """Parse a datetime from various input formats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return None


def _int_or_none(value: object) -> int | None:
    """Convert a value to int or None."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _float_or_none(value: object) -> float | None:
    """Convert a value to float or None."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None