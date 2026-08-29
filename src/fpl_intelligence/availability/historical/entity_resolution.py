"""Entity resolution for historical availability sources (Phase 7.2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from fpl_intelligence.db.models import Player, PlayerExternalId, Season, TeamExternalId
from fpl_intelligence.entity_resolution.resolver import EntityResolutionIssue, normalize_name

# FPL element IDs are shared across the PIT fplcache bootstrap snapshots and
# the existing official/live FPL namespaces. PIT never relies on names alone.
_FPL_PROVIDER_ALIASES: dict[str, tuple[str, ...]] = {
    "real_fpl": ("real_fpl_bootstrap", "fplcache_pit"),
    "real_fpl_bootstrap": ("real_fpl", "fplcache_pit"),
    "fplcache_pit": ("real_fpl", "real_fpl_bootstrap"),
}


def _provider_alias_candidates(provider_name: str) -> tuple[str, ...]:
    return (provider_name,) + _FPL_PROVIDER_ALIASES.get(provider_name, ())


@dataclass
class HistoricalResolutionReport:
    matched_players: int = 0
    unmatched_players: list[EntityResolutionIssue] = field(default_factory=list)
    ambiguous_players: list[EntityResolutionIssue] = field(default_factory=list)
    matched_teams: int = 0
    unmatched_teams: list[EntityResolutionIssue] = field(default_factory=list)
    manual_overrides: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_players": self.matched_players,
            "unmatched_players": [i.__dict__ for i in self.unmatched_players],
            "ambiguous_players": [i.__dict__ for i in self.ambiguous_players],
            "matched_teams": self.matched_teams,
            "unmatched_teams": [i.__dict__ for i in self.unmatched_teams],
            "manual_overrides": self.manual_overrides,
        }


class HistoricalEntityResolver:
    """Resolve provider IDs first; contextual name matching is fallback only."""

    def __init__(self, db: Session, provider_name: str):
        self.db = db
        self.provider_name = provider_name

    def resolve_team(self, provider_team_id: str | None) -> int | None:
        if not provider_team_id:
            return None
        for candidate_provider in _provider_alias_candidates(self.provider_name):
            ext = self.db.scalar(select(TeamExternalId).where(
                TeamExternalId.provider == candidate_provider,
                TeamExternalId.provider_team_id == str(provider_team_id),
            ))
            if ext is not None:
                return ext.team_id
        return None

    def resolve_player(self, provider_player_id: str | None) -> int | None:
        if not provider_player_id:
            return None
        for candidate_provider in _provider_alias_candidates(self.provider_name):
            ext = self.db.scalar(select(PlayerExternalId).where(
                PlayerExternalId.provider == candidate_provider,
                PlayerExternalId.provider_player_id == str(provider_player_id),
            ))
            if ext is not None:
                return ext.player_id
        return None

    def resolve_player_by_context(
        self, provider_player_id: str | None, player_name: str | None,
        team_id: int | None, season_id: int | None, report: HistoricalResolutionReport,
    ) -> int | None:
        pid = self.resolve_player(provider_player_id)
        if pid is not None:
            report.matched_players += 1
            return pid
        by_name: dict[str, list[int]] = {}
        if team_id is not None and season_id is not None:
            from fpl_intelligence.db.models import PlayerTeamMembership
            memberships = self.db.execute(select(PlayerTeamMembership).where(
                PlayerTeamMembership.team_id == team_id,
                PlayerTeamMembership.season_id == season_id,
            )).scalars().all()
            for membership in memberships:
                player = self.db.get(Player, membership.player_id)
                if player:
                    normalized = normalize_name(player.web_name)
                    if normalized:
                        by_name.setdefault(normalized, []).append(player.id)
        normalized_name = normalize_name(player_name or "")
        candidates = by_name.get(normalized_name, [])
        if len(candidates) == 1:
            report.matched_players += 1
            return candidates[0]
        if len(candidates) > 1:
            report.ambiguous_players.append(EntityResolutionIssue(
                self.provider_name, provider_player_id or "?", player_name or "?",
                f"{len(candidates)} contextual candidates",
            ))
            return None
        report.unmatched_players.append(EntityResolutionIssue(
            self.provider_name, provider_player_id or "?", player_name or "?",
            "no provider-id and no unique contextual match",
        ))
        return None

    def resolve_season(self, season_code: str | None) -> int | None:
        if not season_code:
            return None
        season = self.db.scalar(select(Season).where(Season.code == season_code))
        return season.id if season else None

    def resolve_gameweek(self, season_id: int | None, gw_num: int | None) -> int | None:
        if season_id is None or gw_num is None:
            return None
        from fpl_intelligence.db.models import Gameweek
        gw = self.db.scalar(select(Gameweek).where(
            Gameweek.season_id == season_id,
            Gameweek.provider_event_id == gw_num,
        ))
        return gw.id if gw else None
