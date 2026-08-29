"""Versioned, source-provenanced historical FPL deadline catalog for PIT validation.

This catalog is validation metadata only. It never writes database deadline rows and
must not be treated as a replacement for an authoritative DB deadline feed in
production. Each cutoff is sourced from a dated Premier League publication.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fpl_intelligence.availability.historical.materialize_pit import DeadlineCutoff


@dataclass(frozen=True)
class VerifiedDeadline:
    season: str
    gameweek: int
    cutoff: datetime
    source_url: str


# All timestamps are UTC equivalents of the Premier League-stated BST deadlines.
VERIFIED_DEADLINES: tuple[VerifiedDeadline, ...] = (
    VerifiedDeadline("2024-25", 1, datetime(2024, 8, 16, 17, 30, tzinfo=UTC), "https://www.premierleague.com/en/news/4051356"),
    VerifiedDeadline("2024-25", 2, datetime(2024, 8, 24, 10, 0, tzinfo=UTC), "https://www.premierleague.com/en/news/4091742"),
    VerifiedDeadline("2024-25", 3, datetime(2024, 8, 31, 10, 0, tzinfo=UTC), "https://www.premierleague.com/en/news/4098895"),
    VerifiedDeadline("2024-25", 4, datetime(2024, 9, 14, 10, 0, tzinfo=UTC), "https://www.premierleague.com/en/news/4110592"),
    VerifiedDeadline("2024-25", 5, datetime(2024, 9, 21, 10, 0, tzinfo=UTC), "https://www.premierleague.com/en/news/4122647"),
    VerifiedDeadline("2025-26", 1, datetime(2025, 8, 15, 17, 30, tzinfo=UTC), "https://www.premierleague.com/en/news/4372757"),
    VerifiedDeadline("2025-26", 2, datetime(2025, 8, 22, 17, 30, tzinfo=UTC), "https://www.premierleague.com/en/news/4386153"),
    VerifiedDeadline("2025-26", 3, datetime(2025, 8, 30, 10, 0, tzinfo=UTC), "https://www.premierleague.com/en/news/4395376"),
    VerifiedDeadline("2025-26", 4, datetime(2025, 9, 13, 10, 0, tzinfo=UTC), "https://www.premierleague.com/en/news/4407550"),
    VerifiedDeadline("2025-26", 5, datetime(2025, 9, 20, 10, 0, tzinfo=UTC), "https://www.premierleague.com/en/news/4413471"),
)


def load_verified_deadline_cutoffs(
    seasons: list[str] | None = None,
    *,
    gw_min: int | None = None,
    gw_max: int | None = None,
    limit: int | None = None,
) -> list[DeadlineCutoff]:
    """Return only source-provenanced validation cutoffs; never invent missing ones."""
    wanted = {season.strip().replace("/", "-") for season in seasons} if seasons else None
    out: list[DeadlineCutoff] = []
    for entry in VERIFIED_DEADLINES:
        normalized = entry.season.strip().replace("/", "-")
        if wanted is not None and normalized not in wanted:
            continue
        if gw_min is not None and entry.gameweek < gw_min:
            continue
        if gw_max is not None and entry.gameweek > gw_max:
            continue
        out.append(DeadlineCutoff(entry.season, entry.gameweek, entry.cutoff))
        if limit is not None and len(out) >= limit:
            break
    return out
