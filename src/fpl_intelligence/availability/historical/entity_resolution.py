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
    """Resolve provider player/team IDs to canonical DB entities."""

    def __init__(self, db: Session, provider_name: str):
        self.db = db
        self.provider_name = provider_name

    # -- teams ------------------------------------------------------------
    def resolve_team(self, provider_team_id: str | None) -> int | None:
        """Resolve a provider team ID to a canonical team ID."""
        if not provider_team_id:
            return None
        ext = self.db.scalar(
            select(TeamExternalId).where(
                TeamExternalId.provider == self.provider_name,
                TeamExternalId.provider_team_id == str(provider_team_id),
            )
        )
        return ext.team_id if ext else None

    # -- players ----------------------------------------------------------
    def resolve_player(self, provider_player_id: str | None) -> int | None:
        """Resolve a provider player ID to a canonical player ID (primary path)."""
        if not provider_player_id:
            return None
        ext = self.db.scalar(
            select(PlayerExternalId).where(
                PlayerExternalId.provider == self.provider_name,
                PlayerExternalId.provider_player_id == str(provider_player_id),
            )
        )
        return ext.player_id if ext else None

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

        by_name: dict[str, list[tuple[str, int]]] = {}
        # Build a name -> candidate map from the player's team+season context.
        if team_id is not None and season_id is not None:
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
            for m in memberships:
                name = normalize_name(
                    self.db.get(Player, m.player_id).web_name
                    if self.db.get(Player, m.player_id)
                    else ""
                )
                if name:
                    by_name.setdefault(name, []).append(("region", m.player_id))

        norm = normalize_name(player_name or "")
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
        from fpl_intelligence.db.models import Gameweek

        gw = self.db.scalar(
            select(Gameweek).where(
                Gameweek.season_id == season_id,
                Gameweek.provider_event_id == gw_num,
            )
        )
        return gw.id if gw else None
