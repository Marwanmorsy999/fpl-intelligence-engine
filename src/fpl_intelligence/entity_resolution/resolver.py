"""Entity resolution for real data.

Real data may arrive from multiple providers with different spellings,
abbreviations, provider IDs, team renames, or transferred players. This module
provides:

* provider-ID mapping (the primary key -- never merge players on name alone);
* deterministic name normalization for cross-provider joins;
* explicit manual override mappings (loaded from JSON);
* an unresolved-entity queue so nothing is silently dropped.

The FPL mirror's ``element`` ID is the authoritative provider player ID;
canonical internal IDs are assigned once in the database. Cross-provider
resolution maps secondary provider IDs (e.g. Understat) to canonical internal
IDs via name + team + position similarity and manual overrides.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MANUAL_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "entity_mappings" / "manual_overrides.json"
)

_TITLE_EXCEPTIONS = {
    "de", "van", "von", "der", "den", "di", "do", "da", "dos", "das",
    "bin", "el", "al", "ben", "du", "le", "la", "y", "e", "o", "mc", "mac",
}


def normalize_name(name: str) -> str:
    """Normalize a player/team name for fuzzy cross-provider matching."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-zA-Z0-9\s'-]", " ", text)
    words = [w for w in re.split(r"[\s-]+", text.lower()) if w]
    out = []
    for w in words:
        if w in _TITLE_EXCEPTIONS:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


@dataclass
class EntityResolutionIssue:
    """An entity that could not be confidently linked."""

    provider: str
    provider_id: str
    name: str
    reason: str


@dataclass
class ManualOverride:
    provider: str
    source_id: str
    source_name: str
    target_provider: str
    target_id: str
    target_name: str


@dataclass
class EntityResolutionReport:
    matched_players: int = 0
    unmatched_players: list[EntityResolutionIssue] = field(default_factory=list)
    ambiguous_players: list[EntityResolutionIssue] = field(default_factory=list)
    matched_teams: int = 0
    unmatched_teams: list[EntityResolutionIssue] = field(default_factory=list)
    manual_overrides: list[ManualOverride] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_players": self.matched_players,
            "unmatched_players": [i.__dict__ for i in self.unmatched_players],
            "ambiguous_players": [i.__dict__ for i in self.ambiguous_players],
            "matched_teams": self.matched_teams,
            "unmatched_teams": [i.__dict__ for i in self.unmatched_teams],
            "manual_overrides": [m.__dict__ for m in self.manual_overrides],
        }
def load_manual_overrides(path: Path = DEFAULT_MANUAL_PATH) -> list[ManualOverride]:
    """Load manual override mappings from JSON (empty if absent)."""
    if not path.exists():
        return []
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    overrides = []
    for item in data.get("overrides", []):
        overrides.append(
            ManualOverride(
                provider=item.get("provider", ""),
                source_id=str(item.get("source_id", "")),
                source_name=item.get("source_name", ""),
                target_provider=item.get("target_provider", ""),
                target_id=str(item.get("target_id", "")),
                target_name=item.get("target_name", ""),
            )
        )
    return overrides


def resolve_by_name(
    provider_player_id: str,
    name: str,
    by_name: dict[str, list[tuple[str, int]]],
    report: EntityResolutionReport,
    provider: str,
    manual: dict[tuple[str, str], int] | None = None,
) -> int | None:
    """Resolve a secondary-provider player to a canonical internal ID.

    Manual overrides take highest priority; then unique normalized-name match.
    Ambiguity (multiple candidates) is reported, never guessed.
    """
    norm = normalize_name(name)
    if not norm:
        report.unmatched_players.append(
            EntityResolutionIssue(provider, provider_player_id, name, "blank name")
        )
        return None

    if manual is not None:
        manual_key = (provider, str(provider_player_id))
        if manual_key in manual:
            report.matched_players += 1
            return manual[manual_key]

    candidates = by_name.get(norm, [])
    if not candidates:
        report.unmatched_players.append(
            EntityResolutionIssue(
                provider, provider_player_id, name, "no normalized-name match"
            )
        )
        return None
    if len(candidates) > 1:
        report.ambiguous_players.append(
            EntityResolutionIssue(
                provider, provider_player_id, name, f"{len(candidates)} candidates"
            )
        )
        return None
    internal = candidates[0][1]
    report.matched_players += 1
    return internal

