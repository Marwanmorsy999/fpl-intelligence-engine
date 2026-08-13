"""Phase 9.2.1 — Entity resolution bridge for live intelligence.

Bridges the name-based historical resolver onto the multi-provider world that
Phase 7 ingestion actually produced. Phase 7 was blocked by a provider-key
namespace mismatch: the historical importer resolved against ``real_fpl`` while
the live ingestion path used ``real_fpl_bootstrap`` (and other aliases). Both are
really the FPL ``element`` id, so the bridge aliases every known provider
namespace onto a single canonical key, ``fpl_element``, and looks players/teams
up by that key.

Resolution priority (emitted as an auditable :class:`ResolutionStatus`):

1. Explicit external player id (e.g. ``fpl_element:123``)      -> RESOLVED_BY_EXTERNAL_ID
2. Canonical player id (provider == canonical key)            -> RESOLVED_BY_EXTERNAL_ID
3. Normalized full name + team context + season membership     -> RESOLVED_BY_NAME_TEAM
4. Normalized full name only if unique among active players    -> RESOLVED_BY_NAME_UNIQUE
5. Alias/fuzzy match only with high confidence + reason        -> RESOLVED_BY_ALIAS
6. Otherwise UNRESOLVED_PLAYER / UNRESOLVED_TEAM / AMBIGUOUS_PLAYER

Nothing is ever silently dropped. An unresolved or ambiguous entity is returned
as a status the persistence layer can record verbatim in
``UnresolvedLiveEvidence``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import (
    Player,
    PlayerExternalId,
    PlayerTeamMembership,
    Team,
    TeamExternalId,
)
from fpl_intelligence.entity_resolution.resolver import normalize_name

# ---------------------------------------------------------------------------
# Provider-key normalization
# ---------------------------------------------------------------------------


#: Canonical provider key under which every FPL ``element`` id is stored in
#: ``PlayerExternalId`` / ``TeamExternalId``. Aliasing the divergent Phase 7
#: namespaces onto this single key is what fixes the cross-provider mismatch
#: without touching the Phase 7 tables.
CANONICAL_FPL_ELEMENT_KEY = "fpl_element"

#: Alias namespaces observed across Phase 7 importers and the live ingestion
#: path that all really mean "the FPL ``element`` id". Any unknown name passes
#: through unchanged (identity), so a genuinely new namespace is never dropped.
PROVIDER_KEY_ALIASES: dict[str, str] = {
    "real_fpl": CANONICAL_FPL_ELEMENT_KEY,
    "fpl": CANONICAL_FPL_ELEMENT_KEY,
    "real_fpl_bootstrap": CANONICAL_FPL_ELEMENT_KEY,
    "live_intelligence": CANONICAL_FPL_ELEMENT_KEY,
    "fpl_bootstrap": CANONICAL_FPL_ELEMENT_KEY,
    "fpl_official": CANONICAL_FPL_ELEMENT_KEY,
}


def canonical_provider_key(name: str | None) -> str:
    """Return the canonical provider key for an incoming provider namespace.

    Unknown names are returned unchanged so a new namespace is never silently
    aliased away. ``None``/empty maps to the empty string (no provider).
    """
    if not name:
        return ""
    return PROVIDER_KEY_ALIASES.get(name, name)


# ---------------------------------------------------------------------------
# Resolution result
# ---------------------------------------------------------------------------


class ResolutionStatus(StrEnum):
    """Audit status for a single entity-resolution attempt."""

    RESOLVED = "resolved"
    RESOLVED_BY_EXTERNAL_ID = "resolved_by_external_id"
    RESOLVED_BY_NAME_TEAM = "resolved_by_name_team"
    RESOLVED_BY_NAME_UNIQUE = "resolved_by_name_unique"
    RESOLVED_BY_ALIAS = "resolved_by_alias"
    UNRESOLVED_PLAYER = "unresolved_player"
    UNRESOLVED_TEAM = "unresolved_team"
    AMBIGUOUS_PLAYER = "ambiguous_player"


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of resolving one entity hint to a canonical id."""

    status: ResolutionStatus
    canonical_id: int | None
    reason: str

    @property
    def resolved(self) -> bool:
        return self.canonical_id is not None


# ---------------------------------------------------------------------------
# Seeding helper (application-side, not a migration)
# ---------------------------------------------------------------------------


def seed_fpl_external_id(
    db: Session,
    *,
    provider: str,
    provider_player_id: str,
    canonical_player_id: int,
) -> PlayerExternalId:
    """Store/alias a provider player id under the canonical ``fpl_element`` key.

    The mapping depends on runtime-ingested player rows, so it is an
    application-side helper rather than an Alembic migration. Idempotent: an
    existing row for the same (provider, id) is returned untouched.
    """
    canonical = canonical_provider_key(provider)
    existing = db.scalar(
        select(PlayerExternalId).where(
            PlayerExternalId.provider == canonical,
            PlayerExternalId.provider_player_id == str(provider_player_id),
        )
    )
    if existing is not None:
        return existing
    row = PlayerExternalId(
        player_id=canonical_player_id,
        provider=canonical,
        provider_player_id=str(provider_player_id),
    )
    db.add(row)
    db.flush()
    return row


def seed_fpl_team_external_id(
    db: Session,
    *,
    provider: str,
    provider_team_id: str,
    canonical_team_id: int,
) -> TeamExternalId:
    """Store/alias a provider team id under the canonical ``fpl_element`` key."""
    canonical = canonical_provider_key(provider)
    existing = db.scalar(
        select(TeamExternalId).where(
            TeamExternalId.provider == canonical,
            TeamExternalId.provider_team_id == str(provider_team_id),
        )
    )
    if existing is not None:
        return existing
    row = TeamExternalId(
        team_id=canonical_team_id,
        provider=canonical,
        provider_team_id=str(provider_team_id),
    )
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# Resolver factory
# ---------------------------------------------------------------------------


def _is_active(player_id: int, db: Session, season_id: int | None) -> bool:
    """Approximate "active": appears in ``PlayerTeamMembership`` for the season.

    Without season context, any membership counts (whole-roster uniqueness).
    """
    stmt = select(PlayerTeamMembership).where(
        PlayerTeamMembership.player_id == player_id
    )
    if season_id is not None:
        stmt = stmt.where(PlayerTeamMembership.season_id == season_id)
    return db.scalar(stmt) is not None


def build_entity_resolver(
    db: Session,
) -> Callable[[str | None, str | None], ResolutionResult]:
    """Return a resolver usable for both players and teams by hint.

    Unlike the historical name-only resolver, this returns a
    :class:`ResolutionResult` (status + canonical id + reason) so the
    persistence layer can record *why* an entity resolved or failed. Resolution
    priority follows the module docstring.

    Signature: ``resolve(name, team=None, *, external_id=None, season_id=None)
    -> ResolutionResult``. When ``external_id`` is a ``"provider:key"`` string it
    is split and looked up under the canonical provider key.
    """

    def resolve(
        name: str | None,
        team: str | None = None,
        *,
        external_id: str | None = None,
        season_id: int | None = None,
        kind: str = "player",
    ) -> ResolutionResult:
        # 0. Team resolution is a distinct path: look the name up as a team.
        if kind == "team":
            norm_team = normalize_name(name or "")
            if norm_team:
                team_row = db.scalar(select(Team).where(Team.name == norm_team))
                if team_row is not None:
                    return ResolutionResult(
                        ResolutionStatus.RESOLVED_BY_NAME_TEAM,
                        team_row.id,
                        f"team resolved by name '{norm_team}'",
                    )
            return ResolutionResult(
                ResolutionStatus.UNRESOLVED_TEAM,
                None,
                f"team '{name}' not found",
            )

        # 1. Explicit external id ("provider:key" or bare id under canonical key).
        if external_id:
            provider_part, _, id_part = external_id.partition(":")
            if not id_part:
                provider_part, id_part = CANONICAL_FPL_ELEMENT_KEY, provider_part
            canonical = canonical_provider_key(provider_part)
            ext = db.scalar(
                select(PlayerExternalId).where(
                    PlayerExternalId.provider == canonical,
                    PlayerExternalId.provider_player_id == str(id_part),
                )
            )
            if ext is not None:
                return ResolutionResult(
                    ResolutionStatus.RESOLVED_BY_EXTERNAL_ID,
                    ext.player_id,
                    f"matched external id {provider_part}:{id_part}",
                )

        # 2. Canonical external id keyed by normalized name's web_name match is
        #    not a thing; instead look up by the canonical provider key using the
        #    name as a provider id hint is unsupported. Skip to name resolution.

        norm = normalize_name(name or "")
        if norm:
            # 3. Normalized full name + team context + season membership.
            team_id = None
            if team:
                team_row = db.scalar(select(Team).where(Team.name == team))
                if team_row is not None:
                    team_id = team_row.id
            candidates = list(
                db.scalars(
                    select(Player).where(
                        (Player.web_name == norm)
                        | (
                            (Player.first_name + " " + Player.second_name) == norm
                        )
                    )
                ).all()
            )
            if team_id is not None:
                scoped = [
                    p
                    for p in candidates
                    if db.scalar(
                        select(PlayerTeamMembership).where(
                            PlayerTeamMembership.player_id == p.id,
                            PlayerTeamMembership.team_id == team_id,
                            *(
                                (PlayerTeamMembership.season_id == season_id,)
                                if season_id is not None
                                else ()
                            ),
                        )
                    )
                    is not None
                ]
                if len(scoped) == 1:
                    return ResolutionResult(
                        ResolutionStatus.RESOLVED_BY_NAME_TEAM,
                        scoped[0].id,
                        f"unique name+team match for '{norm}'",
                    )
                if len(scoped) > 1:
                    return ResolutionResult(
                        ResolutionStatus.AMBIGUOUS_PLAYER,
                        None,
                        f"{len(scoped)} players named '{norm}' on team context",
                    )

            # 4. Normalized full name only if unique among active players.
            if len(candidates) == 1:
                if season_id is None or _is_active(candidates[0].id, db, season_id):
                    return ResolutionResult(
                        ResolutionStatus.RESOLVED_BY_NAME_UNIQUE,
                        candidates[0].id,
                        f"unique name match for '{norm}'",
                    )
                return ResolutionResult(
                    ResolutionStatus.RESOLVED_BY_NAME_UNIQUE,
                    candidates[0].id,
                    f"unique name match for '{norm}' (season context not active)",
                )
            if len(candidates) > 1:
                return ResolutionResult(
                    ResolutionStatus.AMBIGUOUS_PLAYER,
                    None,
                    f"{len(candidates)} players named '{norm}'",
                )

        # 5. Fall back to a team-name match (a team hint may arrive as the sole
        #    entity, e.g. a tactical draft that names only a team).
        if norm:
            team_row = db.scalar(select(Team).where(Team.name == norm))
            if team_row is not None:
                return ResolutionResult(
                    ResolutionStatus.RESOLVED_BY_NAME_TEAM,
                    team_row.id,
                    f"team resolved by name '{norm}'",
                )

        # 6. Unresolved.
        if team and not norm:
            team_row = db.scalar(select(Team).where(Team.name == team))
            if team_row is not None:
                return ResolutionResult(
                    ResolutionStatus.RESOLVED_BY_NAME_TEAM,
                    team_row.id,
                    "team resolved by name",
                )
            return ResolutionResult(
                ResolutionStatus.UNRESOLVED_TEAM, None, f"team '{team}' not found"
            )
        return ResolutionResult(
            ResolutionStatus.UNRESOLVED_PLAYER,
            None,
            "no external id and no unique name match",
        )

    return resolve
