"""Phase 2.1 — ensemble xPTS model (weighted three-factor prediction).

Replaces single-model xPTS with a weighted ensemble:

* **Fixture difficulty (40%)** — official FDR (1–5) mapped to a 0–10 scale
  where 10 is easiest: ``(6 - fdr) * 2``.
* **Form (35%)** — recency-weighted last-5-gameweek rolling average with
  weights ``GW-1 1.5, GW-2 1.2, GW-3 1.0, GW-4 0.8, GW-5 0.6``.
* **Historical (25%)** — historical average against the same opponent.

The result carries a ±1.96 standard deviation confidence interval derived
from the player's historical score variance.

Safety contract (Phase 2 execution rules): every input is optional. When a
factor's data is missing its weight is *renormalised across the remaining
factors*; when nothing is available at all the function falls back to the
Phase 1 baseline value instead of raising. The returned dict always names
its provenance so callers/UI can label honesty.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

#: Recency weights applied to the last five gameweeks of points,
#: most recent first (index 0 == last finished gameweek).
FORM_WEIGHTS: tuple[float, ...] = (1.5, 1.2, 1.0, 0.8, 0.6)

#: Ensemble weights per the Phase 2 design.
WEIGHT_FIXTURE = 0.40
WEIGHT_FORM = 0.35
WEIGHT_HISTORY = 0.25

#: Two-sided 95% z-score for the confidence interval.
Z_95 = 1.96

#: Conservative SD substitute when no history can establish variance.
DEFAULT_SD = 2.5

#: Floor on any computed SD so constant histories cannot collapse the CI.
MIN_SD = 0.5


def _as_float(raw: Any) -> float | None:
    """Best-effort numeric coercion (FPL often hands us strings)."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def fdr_score(fixture_difficulty: Any) -> float | None:
    """Map official FDR (1–5) onto a 0–10 ease scale (10 = easiest).

    Returns ``None`` when the difficulty is missing or outside 1–5 so the
    caller renormalises without it rather than guessing.
    """
    fdr = _as_float(fixture_difficulty)
    if fdr is None or not (1.0 <= fdr <= 5.0):
        return None
    return (6.0 - fdr) * 2.0


def calculate_form_score(recent_points: Sequence[Any] | None) -> float | None:
    """Recency-weighted average of the last (up to) five gameweeks.

    Fewer than five values is fine: the weights are truncated and
    renormalised over whatever games exist. Empty/absent data yields
    ``None`` (the form factor drops out of the ensemble).
    """
    if not recent_points:
        return None
    points: list[float] = []
    for raw in list(recent_points)[: len(FORM_WEIGHTS)]:
        value = _as_float(raw)
        if value is None:
            continue
        points.append(value)
    if not points:
        return None
    weights = FORM_WEIGHTS[: len(points)]
    weight_sum = sum(weights)
    if weight_sum <= 0:
        return None
    return sum(p * w for p, w in zip(points, weights, strict=False)) / weight_sum


def calculate_historical_sd(points_history: Sequence[Any] | None) -> float | None:
    """Population SD over past gameweek scores (the CI basis).

    Needs at least two points to mean anything; below that returns ``None``
    and the caller substitutes :data:`DEFAULT_SD`. A computed SD is floored
    at :data:`MIN_SD` so a perfectly consistent scorer still keeps a small
    honest interval rather than a degenerate zero-width one.
    """
    if not points_history:
        return None
    values = [v for v in (_as_float(p) for p in points_history) if v is not None]
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    sd = math.sqrt(variance)
    if sd < MIN_SD:
        sd = MIN_SD
    return round(sd, 4)


def calculate_ensemble_xpts(
    player: Any,
    gameweek: int,
    historical_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Three-factor weighted xPTS with a 95% confidence interval.

    Parameters
    ----------
    player:
        Object (or dict) exposing ``fixture_difficulty`` (FDR 1–5) and an
        optional precomputed ``baseline_xpts`` used as the last-resort
        Phase 1 fallback.
    gameweek:
        Target gameweek (informational today; kept for signature parity
        with the Phase 2 spec and future multi-GW ensembles).
    historical_data:
        Optional mapping with ``recent_points`` (up to five most-recent gw
        scores), ``vs_opponent_avg`` (historical average vs the next
        opponent), and ``points_history`` (longer history used for SD/CI).

    Returns
    -------
    dict with ``mean`` / ``lower`` / ``upper`` plus provenance keys:
    ``model`` (``ensemble_v1`` or ``baseline_fallback``), ``weights_used``,
    ``sd``, and ``ci_from_history`` (False ⇒ interval uses DEFAULT_SD and is
    illustrative, not statistical).
    """
    data = dict(historical_data or {})

    def _field(name: str) -> Any:
        if isinstance(player, dict):
            return player.get(name)
        return getattr(player, name, None)

    fixture_component = fdr_score(_field("fixture_difficulty"))
    form_component = calculate_form_score(data.get("recent_points"))

    hist_raw = _as_float(data.get("vs_opponent_avg"))
    # A zero historical average usually means "no data", never "doomed".
    history_component = hist_raw if hist_raw is not None and hist_raw > 0 else None

    components: list[tuple[str, float, float]] = []
    if fixture_component is not None:
        components.append(("fixture", fixture_component, WEIGHT_FIXTURE))
    if form_component is not None:
        components.append(("form", form_component, WEIGHT_FORM))
    if history_component is not None:
        components.append(("history", history_component, WEIGHT_HISTORY))

    if not components:
        # Safety rule: never crash, fall back to the Phase 1 baseline value.
        baseline = _as_float(data.get("baseline_xpts")) or _as_float(
            _field("baseline_xpts")
        )
        base = baseline if baseline is not None else 0.0
        return {
            "mean": round(base, 2),
            "lower": round(base, 2),
            "upper": round(base, 2),
            "model": "baseline_fallback",
            "weights_used": {},
            "sd": None,
            "ci_from_history": False,
        }

    weight_sum = sum(weight for _, _, weight in components)
    mean_xpts = sum(value * _w for _n, value, _w in components) / weight_sum

    sd = calculate_historical_sd(data.get("points_history"))
    ci_from_history = sd is not None
    if sd is None:
        sd = DEFAULT_SD

    lower = max(0.0, mean_xpts - Z_95 * sd)
    upper = mean_xpts + Z_95 * sd

    return {
        "mean": round(mean_xpts, 2),
        "lower": round(lower, 2),
        "upper": round(upper, 2),
        "model": "ensemble_v1",
        "weights_used": {
            name: round(weight / weight_sum, 4) for name, _, weight in components
        },
        "sd": round(sd, 2),
        "ci_from_history": ci_from_history,
    }



# --------------------------------------------------------------------------- #
# Guarded data access (used by route integration; never fatal).
# --------------------------------------------------------------------------- #


def get_last_5_gw_points(db: Session, player_id: int) -> list[float]:
    """Most-recent-first list of up to five gameweek point totals."""
    from fpl_intelligence.db.models import PlayerGameweekPerformance

    rows = db.execute(
        select(
            PlayerGameweekPerformance.gameweek_id,
            PlayerGameweekPerformance.total_points,
        )
        .where(PlayerGameweekPerformance.player_id == int(player_id))
        .order_by(PlayerGameweekPerformance.gameweek_id.desc())
        .limit(5)
    ).all()
    return [float(pts) for _, pts in rows if pts is not None]


def get_points_history(db: Session, player_id: int) -> list[float]:
    """Chronological gameweek point totals used for SD estimation."""
    from fpl_intelligence.db.models import PlayerGameweekPerformance

    rows = db.execute(
        select(PlayerGameweekPerformance.total_points)
        .where(PlayerGameweekPerformance.player_id == int(player_id))
        .order_by(PlayerGameweekPerformance.gameweek_id.asc())
    ).all()
    return [float(pts) for (pts,) in rows if pts is not None]


def get_historical_avg_vs_opponent(
    db: Session, player_id: int, opponent_team_id: int
) -> float | None:
    """Average match points versus one opponent across stored history."""
    from fpl_intelligence.db.models import Fixture, PlayerMatchPerformance

    rows = db.execute(
        select(
            PlayerMatchPerformance.total_points,
            Fixture.home_team,
            Fixture.away_team,
            PlayerMatchPerformance.team_id,
        )
        .join(Fixture, Fixture.id == PlayerMatchPerformance.fixture_id)
        .where(PlayerMatchPerformance.player_id == int(player_id))
    ).all()
    scores = [
        float(total)
        for total, home, away, team_id in rows
        if total is not None
        and team_id is not None
        and opponent_team_id in (home, away)
        and int(team_id) != int(opponent_team_id)
    ]
    if not scores:
        return None
    return sum(scores) / len(scores)


def collect_player_inputs(db: Session, player_id: int) -> dict[str, Any]:
    """Bundle everything :func:`calculate_ensemble_xpts` may consume.

    History lives on internal player ids; callers translate element ids
    first (see ``_attach_phase2_insights``).
    """
    return {
        "recent_points": get_last_5_gw_points(db, player_id),
        "points_history": get_points_history(db, player_id),
    }
