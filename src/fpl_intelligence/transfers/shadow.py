"""Phase 27 Gate 0 (T1) — Shadow Squad & FT Valuation engine.

A staged transfer swaps one player out for one in. The valuation is the
projected net EV over the next 3 GWs (materialized predictions only) minus
any hit cost. The shadow squad itself is the current 15 with the swap
applied — the caller can then re-run the full decision pipeline against it
and label the result "STAGED - Not yet pushed to FPL".
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.sync.materialized_models import PredictionCurrentDB

logger = logging.getLogger(__name__)

HIT_COST = 4
HORIZON_GWS = 3


def _xpts_map(db: Session, gameweek: int) -> dict[int, float]:
    try:
        rows = db.execute(
            select(PredictionCurrentDB.element_id, PredictionCurrentDB.expected_points).where(
                PredictionCurrentDB.gameweek == int(gameweek)
            )
        ).all()
        return {int(e): float(x or 0.0) for e, x in rows}
    except Exception as exc:  # noqa: BLE001 — cold table must not 500
        logger.warning("predictions_current read failed gw%s: %s", gameweek, exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return {}


def _next_target_gw(db: Session, fallback: int) -> int:
    # best-effort: use squad gameweek as anchor; full clock resolution is
    # handled by routes that have async context.
    return int(fallback)


def build_shadow_squad(
    player_ids: list[int],
    element_out: int,
    element_in: int,
) -> list[int] | None:
    """Return new 15-list with OUT replaced by IN, or None if invalid."""
    if int(element_out) not in player_ids:
        return None
    if int(element_in) in player_ids:
        return None
    if len(player_ids) != 15:
        return None
    out = [int(element_in) if pid == int(element_out) else int(pid) for pid in player_ids]
    if len(set(out)) != 15:
        return None
    return out


def compute_ft_valuation(
    db: Session,
    *,
    element_in: int,
    element_out: int,
    free_transfers: int,
    start_gw: int,
    catalog: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Projected net EV of IN over OUT across next 3 GWs.

    FT valuation = SUM(xPTS_in - xPTS_out) over materialized GWs - hit_cost.
    hit_cost = 0 when free_transfers >=1 else 4, increments 4 per extra.

    Returns dict with gross, hit, net, horizon details and recommendation.
    """
    horizon = [int(start_gw) + i for i in range(HORIZON_GWS)]
    gross = 0.0
    used_gws: list[int] = []
    gaps: list[int] = []
    per_gw: list[dict[str, Any]] = []
    for gw in horizon:
        m = _xpts_map(db, gw)
        pin = m.get(int(element_in))
        pout = m.get(int(element_out))
        if pin is None or pout is None:
            gaps.append(gw)
            per_gw.append({"gw": gw, "xpts_in": pin, "xpts_out": pout, "delta": None, "note": "missing prediction"})
            continue
        delta = float(pin) - float(pout)
        gross += delta
        used_gws.append(gw)
        per_gw.append({"gw": gw, "xpts_in": round(float(pin), 2), "xpts_out": round(float(pout), 2), "delta": round(delta, 2)})

    # Hit logic: 1 FT covers first transfer
    transfers_needed = 1
    hit_cost = 0
    if int(free_transfers) < transfers_needed:
        hit_cost = (transfers_needed - int(free_transfers)) * HIT_COST
    # If no horizon data at all, note it
    if not used_gws:
        note = "no materialized predictions for horizon GWs — valuation unavailable"
    elif gaps:
        note = f"missing predictions for GWs {gaps} excluded from EV"
    else:
        note = f"EV over GWs {used_gws}"

    net = round(gross - hit_cost, 2)
    gross_r = round(gross, 2)
    if hit_cost > 0:
        # Use inclusive threshold: net >=0 is worth it
        recommendation = "EXECUTE" if net >= 0 else "AVOID"
    else:
        recommendation = "EXECUTE" if gross_r > 0 else ("HOLD" if gross_r == 0 else "AVOID")

    return {
        "element_in": int(element_in),
        "element_out": int(element_out),
        "free_transfers": int(free_transfers),
        "horizon_gws": horizon,
        "used_gws": used_gws,
        "gaps": gaps,
        "per_gw": per_gw,
        "gross_ev": gross_r,
        "hit_cost": hit_cost,
        "net_ev": net,
        "note": note,
        "recommendation": recommendation,
        "how_computed": "FT Valuation = SUM(xPTS_in - xPTS_out) over next 3 GWs (materialized predictions only) - hit cost; missing GWs excluded",
    }


def shadow_metrics(
    db: Session,
    squad: Any,
    shadow_ids: list[int],
    start_gw: int,
) -> dict[str, Any]:
    """Recalculate dashboard metrics for shadow squad (honest, materialized only).

    Returns per-player xPTS-alpha snapshot + aggregated squad EV delta vs
    current squad's recommended XI. Used to repaint Alpha, xPTS, Captaincy
    cards under "STAGED" label without mutating stored state.
    """
    from fpl_intelligence.alpha.service import alpha_score, league_ownership, position_average
    from fpl_intelligence.prediction.live_provider import load_player_catalog

    catalog = load_player_catalog()
    # Need rival picks for ownership — best effort empty when league thin
    rival_picks: dict[str, list[int]] = {}
    try:
        from fpl_intelligence.api.routes.targets import _rival_picks  # noqa: PLC0415

        rival_picks = _rival_picks(db, str(getattr(squad, "session_id", "") or ""))  # type: ignore[arg-type]
    except Exception:
        rival_picks = {}

    # Build xPTS map for start_gw only for per-player metrics
    xpts = _xpts_map(db, int(start_gw))
    pos_of = {pid: int(row["position"]) for pid, row in catalog.items() if row.get("position")}
    if squad.player_positions:
        for pid, pos in squad.player_positions.items():
            pos_of.setdefault(int(pid), int(pos))
    pos_avg = position_average(xpts, pos_of)

    metrics: dict[str, Any] = {}
    for pid in shadow_ids:
        xp = xpts.get(int(pid))
        pos = pos_of.get(int(pid))
        alpha: float | None = None
        edge: float | None = None
        own_p: float | None = None
        if xp is not None and pos in pos_avg:
            sel = catalog.get(int(pid), {}).get("selected_by_percent")
            own, _label = league_ownership(int(pid), rival_picks, sel)
            own_p = own
            a, terms = alpha_score(xp, pos_avg[pos], own)
            alpha = a
            edge = terms["edge"]
        metrics[str(pid)] = {"xpts": xp, "alpha": alpha, "edge": edge, "own_p": own_p}

    # Horizon valuation of the single swap (for banner)
    # Find which pid changed
    original_ids = set(squad.player_ids or [])
    new_ids = set(shadow_ids)
    added = list(new_ids - original_ids)
    removed = list(original_ids - new_ids)
    valuation: dict[str, Any] | None = None
    if added and removed:
        valuation = compute_ft_valuation(
            db,
            element_in=int(added[0]),
            element_out=int(removed[0]),
            free_transfers=int(getattr(squad, "free_transfers", 1) or 1),
            start_gw=int(start_gw),
        )

    # Squad XI xPTS sum comparison (current vs shadow) for one GW
    try:
        from fpl_intelligence.squad.bridge import DecisionOptimizerBridge  # noqa: PLC0415

        # We won't run full optimization here; just sum xPTS of first 11 ids
        # as honest proxy when optimizer not needed for staging preview.
        cur_sum = round(sum(float(xpts.get(int(pid), 0.0)) for pid in (squad.player_ids or [])[:11]), 2)
        shad_sum = round(sum(float(xpts.get(int(pid), 0.0)) for pid in shadow_ids[:11]), 2)
        xi_delta = round(shad_sum - cur_sum, 2)
    except Exception:
        cur_sum = None  # type: ignore[assignment]
        shad_sum = None  # type: ignore[assignment]
        xi_delta = None

    return {
        "label": "STAGED - Not yet pushed to FPL",
        "shadow_ids": shadow_ids,
        "metrics": metrics,
        "valuation": valuation,
        "xi_xpts_current": cur_sum,
        "xi_xpts_shadow": shad_sum,
        "xi_delta": xi_delta,
        "gameweek": int(start_gw),
        "how_computed": "shadow metrics recomputed from materialized predictions_current for staged squad only",
    }
