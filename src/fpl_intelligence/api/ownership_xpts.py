"""Phase 5 — ownership vs expected-points differential.

Identifies players who are statistically undervalued by the FPL
community: their predicted xPTS is high relative to their ownership
percentage. These are the canonical "differential" picks that win
mini-leagues.

Definitions
-----------

- ``ownership_pct`` — fraction of FPL managers who own the player,
  expressed as a percentage (0-100).
- ``xpcts`` — model predicted expected points for the upcoming
  gameweek.
- ``xpcts_per_ownership`` — xPTS divided by ownership percentage. A
  higher value means "more points per ownership point". This is a
  noisy metric on small ownerships (<5%) so callers should filter.
- ``differential`` — boolean flag set when a player exceeds the
  ``DIFFERENTIAL_OWNERSHIP_MAX`` (default 20.0) AND the
  ``DIFFERENTIAL_XPCTS_MIN`` (default 4.0). These thresholds are
  calibrated for the typical 4-6 pts xPTS band.

The module is pure: no I/O. Inputs are plain dicts / sequences. Tests
in ``tests/unit/test_ownership_xpts.py`` exercise every branch.

Anti-leakage
------------

This module does NOT compute ownership predictions; it only computes
the differential view of (predicted_xpts, observed_ownership). The
ownership percentage itself must come from a real FPL bootstrap pull
(``element.selection:``"``ownership_percent````), which is structurally
pre-deadline information.
"""

from __future__ import annotations

from dataclasses import dataclass

DIFFERENTIAL_OWNERSHIP_MAX = 20.0
DIFFERENTIAL_XPCTS_MIN = 4.0


@dataclass(frozen=True)
class OwnershipXPtsRow:
    """One player's ownership-vs-xPTS row."""

    player_id: int
    name: str
    ownership_pct: float
    xpcts: float
    xpcts_per_ownership: float
    differential: bool


def compute_row(
    player_id: int,
    name: str,
    ownership_pct: float,
    xpcts: float,
    *,
    ownership_max: float = DIFFERENTIAL_OWNERSHIP_MAX,
    xpcts_min: float = DIFFERENTIAL_XPCTS_MIN,
) -> OwnershipXPtsRow:
    """Compute the ownership-vs-xPTS view for one player.

    Returns a row with ``differential=True`` only when both the
    ownership is below ``ownership_max`` AND the xPTS exceeds
    ``xpcts_min``. The threshold defaults are tuned for the standard
    FPL gameweek xPTS band (3-8 pts).
    """
    pid = int(player_id)
    own = max(0.0, float(ownership_pct))
    pts = max(0.0, float(xpcts))
    per_own = 0.0 if own <= 0.0 else pts / own
    is_differential = (own <= ownership_max) and (pts >= xpcts_min)
    return OwnershipXPtsRow(
        player_id=pid,
        name=str(name),
        ownership_pct=own,
        xpcts=pts,
        xpcts_per_ownership=per_own,
        differential=bool(is_differential),
    )


def rank_differentials(
    rows: list[OwnershipXPtsRow],
    *,
    limit: int = 10,
) -> list[OwnershipXPtsRow]:
    """Return the top-``limit`` differential rows, ranked by xPTS/own.

    Only rows with ``differential=True`` are returned. Rows are sorted
    descending by ``xpcts_per_ownership``; ties are broken by absolute
    xPTS descending.
    """
    diffs = [r for r in rows if r.differential]
    diffs.sort(
        key=lambda r: (r.xpcts_per_ownership, r.xpcts),
        reverse=True,
    )
    return diffs[: max(0, int(limit))]


def minutes_risk_flag(minutes_p90: float, *, threshold: float = 45.0) -> bool:
    """Return True when the player's p90 minutes fall below the threshold.

    Used by the UI to flag rotation-risk players.
    """
    return float(minutes_p90) < float(threshold)
