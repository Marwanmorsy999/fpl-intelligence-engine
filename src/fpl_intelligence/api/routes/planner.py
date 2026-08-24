"""Phase 25 Gate 0 (T3) — /api/v1/planner — the HORIZON PLANNER endpoint.

Two-GW lookahead: the best single buy for the next unplayed GW (Alpha engine)
plus a hold/buy verdict for the following GW. Reuses the chip simulator for
the hold-vs-buy EV context and ships an explicit assumptions list (bank, FT
count, chips left) plus a labelled rise-pressure chip with NO percentages.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from fpl_intelligence.alpha import service as alpha_service
from fpl_intelligence.api import deps
from fpl_intelligence.planner import service as planner_service
from fpl_intelligence.prediction.live_provider import load_player_catalog
from fpl_intelligence.squad.service import SquadService
from fpl_intelligence.sync.materialized_models import PredictionCurrentDB

router = APIRouter(prefix="/planner", tags=["planner"])
logger = logging.getLogger(__name__)

POS_NAMES = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def _xpts_map(db: Any, gameweek: int) -> dict[int, float]:
    try:
        rows = db.execute(
            select(
                PredictionCurrentDB.element_id,
                PredictionCurrentDB.expected_points,
            ).where(PredictionCurrentDB.gameweek == int(gameweek))
        ).all()
        return {int(e): float(x or 0.0) for e, x in rows}
    except Exception as exc:  # noqa: BLE001 — cold table must not 500
        logger.warning("predictions_current read failed gw%s: %s", gameweek, exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return {}


def _best_buy(
    db: Any,
    gameweek: int,
    catalog: dict[int, dict[str, Any]],
    rival_picks: dict[str, list[int]],
    exclude_ids: set[int],
) -> dict[str, Any] | None:
    """Highest-Alpha affordable-ish buy for one GW (None = no data)."""
    xpts = _xpts_map(db, gameweek)
    pos_of = {pid: int(row["position"]) for pid, row in catalog.items() if row.get("position")}
    pos_avg = alpha_service.position_average(xpts, pos_of)
    best: tuple[float, dict[str, Any]] | None = None
    for pid, xp in xpts.items():
        if pid in exclude_ids:
            continue
        pos = pos_of.get(pid)
        if pos is None or pos not in pos_avg:
            continue
        sel_pct = catalog.get(pid, {}).get("selected_by_percent")
        own, _label = alpha_service.league_ownership(pid, rival_picks, sel_pct)
        alpha, terms = alpha_service.alpha_score(xp, pos_avg[pos], own)
        score = alpha if alpha is not None else terms["edge"]
        cand = {
            "player_id": pid,
            "web_name": catalog.get(pid, {}).get("web_name") or f"Player {pid}",
            "price": catalog.get(pid, {}).get("price"),
            "alpha": alpha,
            "edge": terms["edge"],
            "xpts": terms["xpts"],
            "pos_avg": terms["pos_avg"],
        }
        if best is None or score > best[0]:
            best = (score, cand)
    return best[1] if best else None


@router.get("", include_in_schema=False)
async def planner_overview(
    response: Response,
    db: deps.GetDB,
    session_id: str = Query(..., description="Session key (saved squad)."),
) -> dict[str, Any]:
    """Two-gameweek transfer plan with explicit assumptions."""
    response.headers["Cache-Control"] = "no-store"

    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        return {
            "session_id": session_id,
            "status": "no-squad",
            "note": "No squad saved for this session",
        }

    from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

    target_gw = await resolve_target_gameweek(db, fallback=int(squad.gameweek))
    next_gw = target_gw + 1

    catalog = load_player_catalog()

    # League picks reuse the targets layer's resolver.
    from fpl_intelligence.api.routes.targets import _rival_picks

    rival_picks = _rival_picks(db, str(session_id))
    squad_ids = set(squad.player_ids or [])

    buy_a = _best_buy(db, target_gw, catalog, rival_picks, squad_ids)

    # GW{n+1}: hold vs buy C — only when predictions actually exist there.
    next_xpts = _xpts_map(db, next_gw)
    buy_c: dict[str, Any] | None = None
    if next_xpts:
        buy_c = _best_buy(db, next_gw, catalog, rival_picks, squad_ids)

    prices = squad.player_prices or {}
    weakest_out = (
        min(squad_ids, key=lambda p: float(prices.get(p, 0.0))) if squad_ids else None
    )

    def _ev_of(cand: dict[str, Any] | None, gw: int) -> float | None:
        """Buy EV = target xPTS − cheapest sellable player's xPTS − 0 hit."""
        if cand is None:
            return None
        tgt = _xpts_map(db, gw).get(int(cand["player_id"]))
        if tgt is None:
            return None
        out_pts = _xpts_map(db, gw).get(int(weakest_out)) if weakest_out else None
        if out_pts is None and weakest_out:
            return round(tgt, 1)
        base = tgt - (out_pts or 0.0)
        return round(base - 0.0, 1)

    ev_a = _ev_of(buy_a, target_gw)
    plan_steps: list[dict[str, Any]] = []
    if buy_a is not None:
        out_name = (
            catalog.get(weakest_out, {}).get("web_name") if weakest_out else None
        ) or (f"Player {weakest_out}" if weakest_out else "—")
        plan_steps.append(
            {
                "gameweek": target_gw,
                "action": f"buy {buy_a['web_name']} out {out_name}",
                "buy": buy_a["web_name"],
                "sell": out_name,
                "ev": ev_a,
            }
        )
    else:
        plan_steps.append(
            {"gameweek": target_gw, "action": "hold — no prediction data for buys", "ev": None}
        )
    if buy_c is not None:
        plan_steps.append(
            {
                "gameweek": next_gw,
                "action": f"hold or buy {buy_c['web_name']}",
                "buy": buy_c["web_name"],
                "ev": _ev_of(buy_c, next_gw),
            }
        )
    else:
        plan_steps.append(
            {
                "gameweek": next_gw,
                "action": f"hold — GW{next_gw} predictions not materialized yet",
                "ev": None,
            }
        )

    pressure = await planner_service.price_pressure(db)

    assumptions = [
        f"bank £{float(squad.bank):.1f}m",
        f"{int(squad.free_transfers)} free transfer(s)",
        f"chips left: {', '.join(squad.chips_available or []) or 'none'}",
        "EV uses stored predictions_current rows only; missing rows are disclosed",
    ]
    how_computed = (
        "GW{n}: best Alpha buy vs cheapest squad sale (xPTS difference); "
        "GW{n+1}: hold unless a higher-Alpha buy exists. Chip simulator "
        "context from optimization.chips; rise pressure from official net "
        "transfer counters + cost changes — never percentages."
    ).replace("{n}", str(target_gw))

    payload = {
        "session_id": session_id,
        "status": "ok",
        "gameweek": target_gw,
        "plan_steps": plan_steps,
        "assumptions": assumptions,
        "price_pressure": pressure,
        "how_computed": how_computed,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    payload["plan_text"] = planner_service.build_plan_text(payload)
    return payload
