"""Phase 24 Gate 1 C1 — multi-GW chip planner.

GET /api/v1/chips/plans?session_id=&start_gw=2&horizon=8
Horizon optimizer: simulate chips starting from start_gw over horizon GWs,
rank by projected total points, respect used chips, return top-3 plans.
"""
# ruff: noqa: E501
from __future__ import annotations

import itertools
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from fpl_intelligence.api import deps
from fpl_intelligence.optimization.chips import ChipSimulator
from fpl_intelligence.optimization.domain import SquadState
from fpl_intelligence.optimization.rules import FPLRules
from fpl_intelligence.squad.service import SquadService

router = APIRouter(prefix="/chips", tags=["chips"])
logger = logging.getLogger(__name__)
GetDB = deps.GetDB

# chip type canonical names used by ChipSimulator / FPLRules
CANONICAL_CHIPS = ["wildcard", "free_hit", "bench_boost", "triple_captain"]

def _canonicalize_available(raw: list[str]) -> list[str]:
    out: list[str] = []
    for c in raw or []:
        low = str(c).lower().strip()
        # half-season suffixes like wildcard_1, wildcard_2, etc.
        base = low.split("_")[0] if "_1" in low or "_2" in low else low
        # normalize hyphens/spaces
        base = base.replace("-", "_").replace(" ", "_")
        # map variants
        if base in ("wildcard", "wild_card"):
            out.append("wildcard")
        elif base in ("free_hit", "freehit", "free-hit"):
            out.append("free_hit")
        elif base in ("bench_boost", "benchboost", "bench-boost"):
            out.append("bench_boost")
        elif base in ("triple_captain", "triplecaptain", "triple-captain"):
            out.append("triple_captain")
        elif low in CANONICAL_CHIPS:
            out.append(low)
    # dedupe preserve order but keep duplicates for double chips (2026/27 allows 2 per type)
    # For optimizer allow up to 2 of each if half-season rule
    counts: dict[str, int] = {}
    for c in out:
        counts[c] = counts.get(c, 0) + 1
    # If raw had no duplicates but rules allow double, expand to 2 when enough availability hint?
    # Simpler: keep as is but ensure each chip appears once; ranking will duplicate via assignments
    # For double chips we duplicate entry so both can be assigned
    expanded: list[str] = []
    for chip in CANONICAL_CHIPS:
        n = counts.get(chip, 0)
        # If zero but FPLRules allows 2 per half and we have 1 generic, allow second as optional?
        # Keep single for now; plan enumeration will handle single per type.
        for _ in range(max(1, n) if n else 0):
            if n:
                expanded.append(chip)
                n -= 1
        if counts.get(chip, 0) == 0 and chip in out:
            # already added
            pass
    # fallback: if we collapsed duplicates incorrectly, just use unique
    if not expanded and out:
        expanded = sorted(set(out))
    # For double allowance, duplicate if half-season
    # Keep unique; allow same chip twice via assignments with replacement.  # noqa: E501
    # So return unique set
    uniq: list[str] = []
    for c in out:
        if c not in uniq:
            uniq.append(c)
    return uniq or out

def _domain_squad_from_payload(squad_payload) -> tuple[SquadState, dict]:
    prices = squad_payload.player_prices or {}
    squad_value = sum(prices.get(pid, 8.0) for pid in squad_payload.player_ids)
    rules = FPLRules()
    remaining = list(squad_payload.chips_available or [])
    # normalise remaining
    remaining = [r for r in remaining if isinstance(r, str)]
    domain = SquadState(
        manager_id=1,
        season="2025-26",
        gameweek=int(squad_payload.gameweek),
        squad_players=list(squad_payload.player_ids),
        starting_xi=[],
        bench_order=[],
        captain=int(squad_payload.captain_id),
        vice_captain=int(squad_payload.vice_captain_id),
        bank=float(squad_payload.bank),
        team_value=float(squad_value + float(squad_payload.bank)),
        free_transfers=int(squad_payload.free_transfers),
        rolled_transfers=0,
        transfer_hits=0,
        remaining_chips=remaining,
        active_chips=[],
        transfer_history=[],
        team_value_history=[],
    )
    return domain, {"rules": rules}

def _baseline_xpts(provider, domain: SquadState, gw: int, baseline_positions: dict[int, int] | None) -> dict[str, Any]:  # noqa: E501
    """Estimate baseline XI xPTS for gw using current squad's players."""
    try:
        # Use XI optimizer to pick best 11? Simpler: pick top 11 by expected points
        # Get predictions for the 15
        preds = provider.get_squad_predictions(list(domain.squad_players), [gw])
        gw_preds = preds.get(gw, {})
        if not gw_preds:
            return {"xpts": 0.0, "detail": "no predictions"}
        # sort 15 by xpts
        sorted_pids = sorted(gw_preds.keys(), key=lambda p: gw_preds[p].expected_points, reverse=True)  # noqa: E501
        top11 = sorted_pids[:11]
        baseline = sum(gw_preds[pid].expected_points for pid in top11)
        # captain double
        if top11:
            baseline += max(gw_preds[pid].expected_points for pid in top11)
        # rely on distribution? use simple sum
        return {"xpts": round(float(baseline), 2), "top11": top11}
    except Exception as exc:
        logger.warning("baseline xpts failed gw%s: %s", gw, exc)
        return {"xpts": 0.0, "detail": str(exc)}

@router.get("/plans", include_in_schema=False)
async def chip_plans(
    db: GetDB,
    session_id: str = Query(..., description="Per-user session key"),
    start_gw: int = Query(2, ge=1, le=38, description="Horizon start (spec says GW2)"),
    horizon: int = Query(8, ge=1, le=12, description="Number of GWs to simulate"),
):
    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    # Resolve chips available – spec says respect squad_summary.chips_available
    chips_available = list(squad.chips_available or [])
    canonical = _canonicalize_available(chips_available)
    if not canonical:
        # fallback to all chips if empty (honest: no chips)
        canonical = []

    provider = deps.get_prediction_provider(db)
    domain, _ = _domain_squad_from_payload(squad)
    # Need to set XI/bench for chip simulator: use optimizer or top 11 fallback
    # We'll attempt to derive XI via provider predictions
    # Build baseline XI for horizon start to populate domain.starting_xi/bench
    gw0 = int(start_gw)
    base = _baseline_xpts(provider, domain, gw0, squad.player_positions)
    top11 = base.get("top11") or domain.squad_players[:11]
    bench = [pid for pid in domain.squad_players if pid not in top11][:4]
    domain.starting_xi = list(top11)
    domain.bench_order = list(bench)

    sim = ChipSimulator(provider, FPLRules())

    horizon_gws = list(range(int(start_gw), int(start_gw) + int(horizon)))
    # compute baseline per GW
    baseline_per_gw: dict[int, float] = {}
    for gw in horizon_gws:
        bp = _baseline_xpts(provider, domain, gw, squad.player_positions)
        baseline_per_gw[gw] = float(bp.get("xpts", 0.0))

    baseline_total = round(sum(baseline_per_gw.values()), 2)

    # compute chip gains per GW for each chip type
    chip_gains: dict[str, dict[int, float]] = {c: {} for c in canonical}
    for chip in canonical:
        for gw in horizon_gws:
            try:
                if chip == "bench_boost":
                    ev = sim.evaluate_bench_boost(domain, gw)
                    gain = float(ev.net_value)
                elif chip == "triple_captain":
                    ev = sim.evaluate_triple_captain(domain, gw)
                    gain = float(ev.net_value)
                elif chip == "free_hit":
                    ev = sim.evaluate_free_hit(domain, gw)
                    gain = float(ev.net_value)
                elif chip == "wildcard":
                    ev = sim.evaluate_wildcard(domain, gw, horizon=4)
                    gain = float(ev.net_value)
                    # spread wc gain over 4 GWs? for ranking we treat as single GW gain
                    # but per-GW breakdown we will show wc GW gain + followed holds
                else:
                    gain = 0.0
                # clamp negative gains to 0 (don't play chip for loss)
                if gain < 0:
                    gain = 0.0
                chip_gains[chip][gw] = round(gain, 2)
            except Exception as exc:
                logger.warning("chip gain failed %s gw%s: %s", chip, gw, exc)
                chip_gains[chip][gw] = 0.0

    # Generate candidate plans: enumerate placements of chips to GWs.  # noqa: E501
    # At most one chip per GW; brute force via product enumeration.  # noqa: E501
    # with pruning where multiple chips share GW => invalid.
    # We also consider that same chip type could be used twice if double allowance? For now single use per type.
    best_plans: list[dict[str, Any]] = []
    # If no chips, return baseline plan
    if not canonical:
        best_plans.append({
            "label": "No chips available — hold",
            "chips": [],
            "total_ev": baseline_total,
            "total_gain": 0.0,
            "breakdown": [{"gameweek": gw, "action": "hold", "chip": None, "xpts": baseline_per_gw[gw], "gain": 0.0} for gw in horizon_gws],
        })
    else:
        # Generate all plans: for each subset of chips and each assignment to GWs
        # Limit horizon to 8, chips up to 4 -> total combinations ~ (8+1)^4 ~ 6561, still fine
        # We'll iterate over product of (horizon_gws + [None]) for each chip
        chips_list = list(canonical)
        n = len(chips_list)
        # product space
        options = horizon_gws + [None]  # None = not played
        seen_signatures: set[tuple] = set()
        for assignment in itertools.product(options, repeat=n):
            # build chip->gw map where not None
            used_gws = [gw for gw in assignment if gw is not None]
            if len(used_gws) != len(set(used_gws)):
                continue  # two chips same GW not allowed
            # signature for dedupe (sorted by chip placement)
            sig = tuple(sorted((chips_list[i], assignment[i]) for i in range(n) if assignment[i] is not None))
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            # compute total gain
            total_gain = 0.0
            breakdown: list[dict[str, Any]] = []
            # need per GW chip lookup
            gw_to_chip: dict[int, str] = {}
            for i, gw in enumerate(assignment):
                if gw is not None:
                    gw_to_chip[int(gw)] = chips_list[i]
            for gw in horizon_gws:
                chip = gw_to_chip.get(gw)
                base_x = baseline_per_gw[gw]
                if chip:
                    gain = float(chip_gains.get(chip, {}).get(gw, 0.0))
                    total_gain += gain
                    breakdown.append({"gameweek": gw, "action": f"play {chip}", "chip": chip, "xpts": round(base_x + gain, 2), "gain": round(gain, 2)})
                else:
                    breakdown.append({"gameweek": gw, "action": "hold", "chip": None, "xpts": round(base_x, 2), "gain": 0.0})
            total_ev = round(baseline_total + total_gain, 2)
            # Skip plans that play no chips if we have chips (baseline is 0 gain)
            # Keep at least one "no chips" plan for reference
            label_parts: list[str] = []
            for gw in sorted(gw_to_chip.keys()):
                chip = gw_to_chip[gw]
                label_parts.append(f"{chip.replace('_',' ').title()} GW{gw}")
            label = " + ".join(label_parts) if label_parts else "Hold (no chip)"
            if label_parts:
                label += f" = +{round(total_gain,1)} EV"
            best_plans.append({
                "label": label,
                "chips": [{"chip": chip, "gw": gw} for gw, chip in sorted(gw_to_chip.items())],
                "total_ev": total_ev,
                "total_gain": round(total_gain, 2),
                "breakdown": breakdown,
            })
        # rank by total_gain desc, then total_ev
        best_plans.sort(key=lambda p: (-float(p["total_gain"]), -float(p["total_ev"])))
        # keep top 3 non-zero plus baseline? Spec says show top-3 plans even if some are zero? Keep top 3 by gain.
        # Ensure we have at least 3 plans: if less than 3, pad with hold
        if len(best_plans) < 3:
            while len(best_plans) < 3:
                best_plans.append({
                    "label": f"Hold (no chip) #{len(best_plans)+1}",
                    "chips": [],
                    "total_ev": baseline_total,
                    "total_gain": 0.0,
                    "breakdown": [{"gameweek": gw, "action": "hold", "chip": None, "xpts": baseline_per_gw[gw], "gain": 0.0} for gw in horizon_gws],
                })
        # But ensure worst plan isn't duplicate hold
        # Take top 3 by gain, but if top gain is 0, still show holds
        best_plans = best_plans[:3]

    # Honest note when chips are empty
    note = ""
    if not chips_available:
        note = "No chips available — all used."
    elif best_plans and best_plans[0]["total_gain"] == 0:
        note = "Engine sees no positive-EV chip window in this horizon."

    return {
        "session_id": session_id,
        "start_gw": int(start_gw),
        "horizon": int(horizon),
        "chips_available": canonical,
        "chips_available_raw": chips_available,
        "baseline_total": baseline_total,
        "baseline_per_gw": baseline_per_gw,
        "plans": best_plans,
        "note": note,
    }
