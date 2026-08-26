"""Phase 2.3 — dynamic captain-confidence score (replaces the static 70%).

Multi-factor confidence for the armband pick:

* **xPTS margin (40%)**      — gap to the second-best option, ``min(10,
                               margin * 2)`` so any gap ≥ 5 saturates.
* **Fixture ease (30%)**     — ``5 - FDR`` on the official 1–5 scale.
* **Form ratio (20%)**       — ``form_avg / season_avg`` scaled by 20;
                               1.0 (in-form == season average) contributes
                               exactly 4.0 raw points.
* **Ownership risk (10%)**   — ``10 - selected_by_percent / 10``; template
                               picks (high ownership) score low here.

The weighted sum is scaled ×10 and clamped into **[50, 95]**: a captain is
never presented as a coin flip, and nothing is ever "certain".

Safety contract: every factor is optional. A factor whose inputs are missing
or degenerate (e.g. ``season_avg == 0``) drops out and its weight is
renormalised across the survivors. With NO usable factors the score falls
back to the 50% floor and the caller can tell via ``captain_confidence_detail``
(``"complete": false``).
"""

from __future__ import annotations

from typing import Any

#: Factor weights (Phase 2 design; sum to 1.0 when every factor is usable).
WEIGHT_MARGIN = 0.40
WEIGHT_FIXTURE = 0.30
WEIGHT_FORM = 0.20
WEIGHT_OWNERSHIP = 0.10

#: Output clamp (percent).
CONFIDENCE_FLOOR = 50.0
CONFIDENCE_CEILING = 95.0

#: Margin points above which the margin factor saturates (min(10, margin*2)).
MARGIN_SATURATION = 10.0


def _as_float(raw: Any) -> float | None:
    """Best-effort numeric coercion (FPL hands us strings constantly)."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _field(player: Any, name: str) -> Any:
    """Read ``name`` off a dict-like or attribute-style pick."""
    if isinstance(player, dict):
        return player.get(name)
    return getattr(player, name, None)


def margin_score_value(top_xpts: Any, second_xpts: Any) -> float | None:
    """Saturated xPTS-gap score: ``min(10, (top - second) * 2)``.

    Both xPTS values are required; a negative gap passes through honestly
    (it drags the score toward the floor — captaincy genuinely doubts it).
    """
    top = _as_float(top_xpts)
    second = _as_float(second_xpts)
    if top is None or second is None:
        return None
    return min(MARGIN_SATURATION, (top - second) * 2.0)


def fixture_ease_value(fixture_difficulty: Any) -> float | None:
    """``5 - FDR`` for official FDR in 1–5; ``None`` outside that range."""
    fdr = _as_float(fixture_difficulty)
    if fdr is None or not (1.0 <= fdr <= 5.0):
        return None
    return 5.0 - fdr


def form_contribution_value(form_avg: Any, season_avg: Any) -> float | None:
    """Form-ratio contribution ``ratio * 20`` (ratio 1.0 ⇒ exactly 4.0 raw).

    Needs a positive, finite season average; degenerate inputs drop the
    factor rather than divide by zero.
    """
    form = _as_float(form_avg)
    season = _as_float(season_avg)
    if form is None or season is None or season <= 0:
        return None
    return (form / season) * 20.0


def ownership_risk_value(selected_by_percent: Any) -> float | None:
    """``10 - selected_by_percent / 10`` — differential captains score high."""
    pct = _as_float(selected_by_percent)
    if pct is None:
        return None
    return 10.0 - (pct / 10.0)


def _build_factors(
    top_pick: Any, second_pick: Any | None
) -> tuple[list[tuple[str, float, float]], list[str]]:
    """Collect usable ``(name, contribution, weight)`` factors + dropped names."""
    factors: list[tuple[str, float, float]] = []
    dropped: list[str] = []

    margin = margin_score_value(
        _field(top_pick, "xpts"),
        _field(second_pick, "xpts") if second_pick is not None else None,
    )
    if margin is None:
        dropped.append("margin")
    else:
        factors.append(("margin", margin, WEIGHT_MARGIN))

    ease = fixture_ease_value(_field(top_pick, "fixture_difficulty"))
    if ease is None:
        dropped.append("fixture")
    else:
        factors.append(("fixture", ease, WEIGHT_FIXTURE))

    form_term = form_contribution_value(
        _field(top_pick, "form_avg"), _field(top_pick, "season_avg")
    )
    if form_term is None:
        dropped.append("form")
    else:
        factors.append(("form", form_term, WEIGHT_FORM))

    ownership = ownership_risk_value(_field(top_pick, "selected_by_percent"))
    if ownership is None:
        dropped.append("ownership")
    else:
        factors.append(("ownership", ownership, WEIGHT_OWNERSHIP))

    return factors, dropped


def captain_confidence_detail(
    top_pick: Any, second_pick: Any | None, gameweek: int
) -> dict[str, Any]:
    """Full provenance view of the confidence score (score + factor maths).

    ``gameweek`` rides along in the payload for UI labeling; the math itself
    is GW-independent today.
    """
    factors, dropped = _build_factors(top_pick, second_pick)
    if not factors:
        # Nothing measurable — honest floor rather than fabricated certainty.
        return {
            "gameweek": int(gameweek) if gameweek is not None else None,
            "score": round(CONFIDENCE_FLOOR, 1),
            "raw_score": 0.0,
            "factors": {},
            "dropped_factors": ["margin", "fixture", "form", "ownership"],
            "complete": False,
            "clamped": True,
        }

    weight_sum = sum(w for _, _, w in factors)
    raw_score = sum(v * w for _, v, w in factors) / weight_sum
    scaled = raw_score * 10.0
    clamped = not (CONFIDENCE_FLOOR <= scaled <= CONFIDENCE_CEILING)
    score = max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, scaled))

    return {
        "gameweek": int(gameweek) if gameweek is not None else None,
        "score": round(score, 1),
        "raw_score": round(raw_score, 4),
        "factors": {
            name: {"value": round(value, 4), "weight": round(weight / weight_sum, 4)}
            for name, value, weight in factors
        },
        "dropped_factors": dropped,
        "complete": not dropped,
        "clamped": clamped,
    }


def calculate_captain_confidence(
    top_pick: Any, second_pick: Any | None, gameweek: int
) -> float:
    """Dynamic armband confidence percent (spec signature), clamped [50, 95]."""
    detail = captain_confidence_detail(top_pick, second_pick, gameweek)
    return detail["score"]
