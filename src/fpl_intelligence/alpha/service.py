"""Phase 25 Gate 0 (T2) — the ALPHA ENGINE.

Alpha ranks transfer targets by *value above position peers weighted by how
rarely your league already owns them*:

    pos_avg   = mean xPTS of same-position players (predictions_current)
    own_p     = rivals owning p / rivals with cached picks
                (<3 rivals cached -> global selected_by_percent, labelled)
    alpha_p   = (xPTS_p − pos_avg) × (1 − own_p)

Every term is returned so the UI can show both inputs. ``volatility`` is the
population standard deviation of the player's last-5 ACTUAL points from
``ingested_history``, labelled "recent volatility" — never a probability.

Honesty rules: a metric with no data ships as ``None`` and the UI renders an
"unavailable" chip. No invented numbers anywhere.
"""

from __future__ import annotations

import logging
import statistics
from typing import Any

logger = logging.getLogger(__name__)

#: Below this many cached rival picks, ownership falls back to global %.
MIN_LEAGUE_RIVALS = 3


def position_average(xpts_by_element: dict[int, float], pos_of: dict[int, int]) -> dict[int, float]:
    """Mean xPTS per FPL position code (1..4) over predicted players (pure)."""
    buckets: dict[int, list[float]] = {}
    for pid, xpts in xpts_by_element.items():
        pos = pos_of.get(int(pid))
        if pos is None:
            continue
        buckets.setdefault(int(pos), []).append(float(xpts))
    return {
        pos: round(statistics.fmean(vals), 3) for pos, vals in buckets.items() if vals
    }


def league_ownership(
    player_id: int,
    rival_picks: dict[str, list[int]],
    global_selected_by: str | None,
) -> tuple[float | None, str]:
    """(ownership_fraction_or_None, label) for one player (pure).

    League share when >= MIN_LEAGUE_RIVALS rivals have cached picks; else the
    global selected-by percentage (labelled "global ownership (league data
    thin)"). Returns (None, "unavailable") when neither source exists.
    """
    if len(rival_picks) >= MIN_LEAGUE_RIVALS:
        owners = sum(
            1 for ids in rival_picks.values() if int(player_id) in {int(p) for p in ids}
        )
        return owners / len(rival_picks), "league ownership"
    if global_selected_by:
        try:
            pct = float(str(global_selected_by).replace("%", "").strip())
        except ValueError:
            return None, "unavailable"
        return pct / 100.0, "global ownership (league data thin)"
    return None, "unavailable"


def alpha_score(
    xpts: float, pos_avg: float, own_p: float | None
) -> tuple[float | None, dict[str, Any]]:
    """Alpha + its two displayed terms (pure).

    Without an ownership number the multiplier term cannot be computed
    honestly, so Alpha itself becomes unavailable while the raw edge
    (xPTS − pos_avg) still renders.
    """
    edge = round(float(xpts) - float(pos_avg), 2)
    terms = {"xpts": round(float(xpts), 2), "pos_avg": round(float(pos_avg), 2), "edge": edge}
    if own_p is None:
        return None, {**terms, "own_p": None}
    own = max(0.0, min(1.0, float(own_p)))
    alpha = round(edge * (1.0 - own), 3)
    return alpha, {**terms, "own_p": round(own, 4)}


def recent_volatility(points_history: list[int]) -> float | None:
    """Population std-dev of last-5 actual points; None under 2 samples."""
    sample = [float(p) for p in points_history[-5:] if p is not None]
    if len(sample) < 2:
        return None
    return round(statistics.pstdev(sample), 2)


def position_need_boost(counts: dict[int, int]) -> dict[int, float]:
    """Transparent thinness weight per position (pure).

    weight_pos = 1 + 0.2 × (max_count − count_pos) / max_count, so the
    thinnest rostered position gets up to +20% ranking boost and a full
    position gets exactly 1.0. Documented constant, not tuned per request.
    """
    weights: dict[int, float] = {}
    max_count = max((c for c in counts.values() if c > 0), default=0)
    if max_count <= 0:
        return {}
    for pos in (1, 2, 3, 4):
        count = counts.get(pos, 0)
        weights[pos] = round(1.0 + 0.2 * ((max_count - count) / max_count), 4)
    return weights
