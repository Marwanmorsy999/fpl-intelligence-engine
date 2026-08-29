"""Entity resolution for historical availability sources (Phase 7.2).

Maps provider entities to canonical players/teams/seasons/gameweeks. We never
match players by name alone. Resolution priority:

1. provider player ID (via PlayerExternalId / TeamExternalId)
2. canonical mapping (provider_event_id -> canonical id)
3. team context (player belongs to a team we can resolve)
4. season context
5. name as supporting evidence only

A reconciliation report is generated (matched / unmatched / ambiguous /
manually mapped). Unresolved entities are never silently dropped; they are
recorded and surfaced in the coverage audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import (
    Player,
    PlayerExternalId,
    Season,
    TeamExternalId,
)
from fpl_intelligence.entity_resolution.resolver import (
    EntityResolutionIssue,
    normalize_name,
)

#: Provider aliases for the official FPL element-ID namespace. The PIT
#: fplcache provider contains the same numeric element IDs as the existing
#: official/live FPL namespaces, so ID lookup remains authoritative without
#: weakening the resolver to name-only matching.
_FPL_PROVIDER_ALIASES: dict[str, tuple[str, ...]] = {
    "real_fpl": ("real_fpl_bootstrap", "fplcache_pit"),
    "real_fpl_bootstrap": ("real_fpl", "fplcache_pit"),
    "fplcache_pit": ("real_fpl", "real_fpl_bootstrap"),
}


def _provider_alias_candidates(provider_name: str) -> tuple[str, ...]:
    """Return provider names to query for ID lookup, primary first."""
    aliases = _FPL_PROVIDER_ALIASES.get(provider_name, ())
    return (provider_name,) + aliases


@dataclass
class HistoricalResolutionReport:
    """Reconciliation report for a historical availability import."""

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
    """Resolve provider player/team IDs to canonical DB entities.

    Resolver lookups are memoized for the lifetime of one import transaction.
    Historical PIT batches repeat the same player/team/gameweek identifiers
    across snapshots, so caching avoids thousands of redundant round trips
    without changing matching precedence or fallback semantics.
    """

    def __init__(self, db: Session, provider_name: str):
        self.db = db
        self.provider_name = provider_name
        self._team_cache: dict[str, int | None] = {}
        self._player_cache: dict[str, int | None] = {}
        self._gameweek_cache: dict[tuple[int, int], int | None] = {}
        self._context_name_cache: dict[tuple[int, int], dict[str, list[tuple[str, int]]]] = {}

    # -- teams ------------------------------------------------------------
    def resolve_team(self, provider_team_id: str | None) -> int | None:
        """Resolve a provider team ID to a canonical team ID."""
        if not provider_team_id:
            return None
        key = str(provider_team_id)
        if key in self._team_cache:
            return self._team_cache[key]
        resolved: int | None = None
        for candidate_provider in _provider_alias_candidates(self.provider_name):
            ext = self.db.scalar(
                select(TeamExternalId).where(
                    TeamExternalId.provider == candidate_provider,
                    TeamExternalId.provider_team_id == key,
                )
            )
            if ext is not None:
                resolved = ext.team_id
                break
        self._team_cache[key] = resolved
        return resolved

    # -- players ----------------------------------------------------------
    def resolve_player(self, provider_player_id: str | None) -> int | None:
        """Resolve a provider player ID to a canonical player ID (primary path)."""
        if not provider_player_id:
            return None
        key = str(provider_player_id)
        if key in self._player_cache:
            return self._player_cache[key]
        resolved: int | None = None
        for candidate_provider in _provider_alias_candidates(self.provider_name):
            ext = self.db.scalar(
                select(PlayerExternalId).where(
                    PlayerExternalId.provider == candidate_provider,
                    PlayerExternalId.provider_player_id == key,
                )
            )
            if ext is not None:
                resolved = ext.player_id
                break
        self._player_cache[key] = resolved
        return resolved

    def _context_candidates(
        self,
        team_id: int,
        season_id: int,
    ) -> dict[str, list[tuple[str, int]]]:
        key = (team_id, season_id)
        cached = self._context_name_cache.get(key)
        if cached is not None:
            return cached

        from fpl_intelligence.db.models import PlayerTeamMembership

        memberships = (
            self.db.execute(
                select(PlayerTeamMembership).where(
                    PlayerTeamMembership.team_id == team_id,
                    PlayerTeamMembership.season_id == season_id,
                )
            )
            .scalars()
            .all()
        )
        by_name: dict[str, list[tuple[str, int]]] = {}
        for membership in memberships:
            player = self.db.get(Player, membership.player_id)
            name = normalize_name(player.web_name if player else "")
            if name:
                by_name.setdefault(name, []).append(("region", membership.player_id))
        self._context_name_cache[key] = by_name
        return by_name

    def resolve_player_by_context(
        self,
        provider_player_id: str | None,
        player_name: str | None,
        team_id: int | None,
        season_id: int | None,
        report: HistoricalResolutionReport,
    ) -> int | None:
        """Resolve a player using provider-ID first, then team+season+name context.

        Provider-ID is the authoritative primary key. Only when it is absent do
        we fall back to contextual matching (team context + season + normalized
        name as supporting evidence). Ambiguity is reported, never guessed.
        """
        pid = self.resolve_player(provider_player_id)
        if pid is not None:
            report.matched_players += 1
            return pid

        norm = normalize_name(player_name or "")
        by_name: dict[str, list[tuple[str, int]]] = {}
        if team_id is not None and season_id is not None:
            by_name = self._context_candidates(team_id, season_id)

        if norm and norm in by_name:
            candidates = by_name[norm]
            if len(candidates) == 1:
                report.matched_players += 1
                return candidates[0][1]
            report.ambiguous_players.append(
                EntityResolutionIssue(
                    self.provider_name,
                    provider_player_id or "?",
                    player_name or "?",
                    f"{len(candidates)} contextual candidates",
                )
            )
            return None

        report.unmatched_players.append(
            EntityResolutionIssue(
                self.provider_name,
                provider_player_id or "?",
                player_name or "?",
                "no provider-id and no unique contextual match",
            )
        )
        return None

    # -- season / gameweek ------------------------------------------------
    def resolve_season(self, season_code: str | None) -> int | None:
        """Resolve a season code (e.g. '2024-25') to a canonical season ID."""
        if not season_code:
            return None
        season = self.db.scalar(select(Season).where(Season.code == season_code))
        return season.id if season else None

    def resolve_gameweek(self, season_id: int | None, gw_num: int | None) -> int | None:
        """Resolve a gameweek number within a season to a canonical gameweek ID."""
        if season_id is None or gw_num is None:
            return None
        key = (season_id, gw_num)
        if key in self._gameweek_cache:
            return self._gameweek_cache[key]
        from fpl_intelligence.db.models import Gameweek

        gw = self.db.scalar(
            select(Gameweek).where(
                Gameweek.season_id == season_id,
                Gameweek.provider_event_id == gw_num,
            )
        )
        resolved = gw.id if gw else None
        self._gameweek_cache[key] = resolved
        return resolved
