"""Phase 21.1 — canonical team-name normalisation shared by every consumer.

The Odds API publishes full club names ("Manchester City"); FPL bootstrap uses
short forms ("Man City") and fans type abbreviations ("MCI"). One table plus
one pure function keeps h2h books, fixture scans and market checks talking
about the same clubs without hardcoded ids.
"""

from __future__ import annotations

import re
import unicodedata

#: Every known spelling variant -> The-Odds-API style canonical name.
TEAM_NAME_ALIASES: dict[str, str] = {
    # abbreviations
    "mci": "Manchester City",
    "mun": "Manchester United",
    "liv": "Liverpool",
    "ars": "Arsenal",
    "che": "Chelsea",
    "tot": "Tottenham Hotspur",
    "new": "Newcastle United",
    "eve": "Everton",
    "whu": "West Ham United",
    "wol": "Wolverhampton Wanderers",
    "bre": "Brentford",
    "cry": "Crystal Palace",
    "avl": "Aston Villa",
    "bou": "AFC Bournemouth",
    "ful": "Fulham",
    "bur": "Burnley",
    "sun": "Sunderland",
    "lee": "Leeds United",
    "nfo": "Nottingham Forest",
    "bha": "Brighton & Hove Albion",
    # FPL / broadcast short forms
    "man city": "Manchester City",
    "man utd": "Manchester United",
    "man united": "Manchester United",
    "manchester utd": "Manchester United",
    "spurs": "Tottenham Hotspur",
    "tottenham": "Tottenham Hotspur",
    "nott'm forest": "Nottingham Forest",
    "nottm forest": "Nottingham Forest",
    "nott m forest": "Nottingham Forest",
    "wolves": "Wolverhampton Wanderers",
    "wolverhampton": "Wolverhampton Wanderers",
    "brighton": "Brighton & Hove Albion",
    "brighton and hove albion": "Brighton & Hove Albion",
    "bournemouth": "AFC Bournemouth",
    "west ham": "West Ham United",
    "newcastle": "Newcastle United",
    "leeds": "Leeds United",
    "burnley": "Burnley",
    "fulham": "Fulham",
    "everton": "Everton",
    "sunderland": "Sunderland",
    "brentford": "Brentford",
    "crystal palace": "Crystal Palace",
    "aston villa": "Aston Villa",
    "arsenal": "Arsenal",
    "chelsea": "Chelsea",
    "liverpool": "Liverpool",
    "manchester city": "Manchester City",
    "manchester united": "Manchester United",
    "tottenham hotspur": "Tottenham Hotspur",
    "nottingham forest": "Nottingham Forest",
    "west ham united": "West Ham United",
    "wolverhampton wanderers": "Wolverhampton Wanderers",
}

_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def fold_team_name(name: str | None) -> str:
    """Lowercase, accent-free, punctuation-stripped key ("Nott'm Forest" -> "nottm forest")."""
    lowered = _strip_accents((name or "").strip()).lower()
    return _NON_WORD_RE.sub(" ", lowered).strip()


def canonical_team_name(name: str | None) -> str:
    """Map any spelling variant onto the canonical bookmaker-style name.

    Unknown names pass through folded so matching degrades to a plain string
    comparison instead of failing loudly.
    """
    key = fold_team_name(name)
    if not key:
        return ""
    mapped = TEAM_NAME_ALIASES.get(key)
    if mapped:
        return mapped
    # Alias keys are stored with their natural punctuation; try the raw lower
    # form too so entries like "nott'm forest" hit even before folding.
    raw = _strip_accents((name or "").strip()).lower()
    mapped = TEAM_NAME_ALIASES.get(raw)
    return mapped or key
