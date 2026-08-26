"""Phase 25 Gate 0 (T1) — transfer intelligence API + Phase 27 Shadow Squad.

``GET /api/v1/transfers/ledger?entry_id=`` returns the materialized ledger
with horizon EV per row and the honest source label. ``GET
/api/v1/transfers/detected?session_id=`` powers the on-sync banner when a
snapshot change implies a transfer.

Phase 27 Gate 0 adds:
* ``GET /shadow`` — staged transfer valuation + shadow squad metrics
                 (labelled "STAGED - Not yet pushed to FPL").
* ``GET /valuation`` — FT Valuation over next 3 GWs vs keeping player.

v2.7.2-planner-only: POST /execute removed. App is planner-only: FPL is the
execution layer. Use the "View on FPL" button and Sync Now to pull changes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from fpl_intelligence.api import deps
from fpl_intelligence.transfers import service as transfer_service

router = APIRouter(prefix="/transfers", tags=["transfers"])
logger = logging.getLogger(__name__)


@router.get("/ledger", include_in_schema=False)
async def transfers_ledger(
    response: Response,
    db: deps.GetDB,
    entry_id: str = Query(..., description="FPL entry id."),
) -> dict[str, Any]:
    """Official-first transfer ledger; honest unavailable state otherwise."""
    response.headers["Cache-Control"] = "no-store"
    if not str(entry_id).strip().isdigit():
        return {
            "entry_id": entry_id,
            "status": "unavailable",
            "note": "entry id must be numeric",
            "transfers": [],
            "count": 0,
        }
    return await transfer_service.build_ledger(db, entry_id)


@router.get("/detected", include_in_schema=False)
async def transfers_detected(
    response: Response,
    db: deps.GetDB,
    session_id: str = Query(..., description="FPL entry id (= session key)."),
) -> dict[str, Any]:
    """Latest snapshot-diffed transfer, or an explicit none-detected state."""
    response.headers["Cache-Control"] = "no-store"
    detected = transfer_service.detect_transfer_between_snapshots(db, session_id)
    return {"session_id": session_id, "detected": detected}


# --------------------------------------------------------------------------- #
# Phase 27 Gate 0 — Shadow Squad (T1) + FT Valuation (S3)
# v2.7.2-planner-only — Direct Push (T2) / POST /execute removed.
# --------------------------------------------------------------------------- #


class ShadowRequest(BaseModel):
    session_id: str = Field(..., description="FPL entry id / session key")
    element_in: int = Field(..., gt=0)
    element_out: int = Field(..., gt=0)


@router.get("/valuation", include_in_schema=False)
async def transfers_valuation(
    response: Response,
    db: deps.GetDB,
    session_id: str = Query(..., description="FPL entry id (= session key)"),
    element_in: int = Query(..., gt=0),
    element_out: int = Query(..., gt=0),
) -> dict[str, Any]:
    """FT Valuation: projected net EV of staged transfer over next 3 GWs.

    v2.7.3-dual-state: reads the *effective* (local-preferred) squad.
    """
    response.headers["Cache-Control"] = "no-store"
    from fpl_intelligence.squad.service import SquadService
    from fpl_intelligence.transfers.shadow import compute_ft_valuation

    squad = SquadService(session=db).get_effective_squad(session_id=session_id)
    if squad is None:
        return {"status": "no-squad", "note": "No squad saved for this session", "valuation": None}
    try:
        from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

        target_gw = await resolve_target_gameweek(db, fallback=int(squad.gameweek))
    except Exception:  # noqa: BLE001
        target_gw = int(squad.gameweek)

    valuation = compute_ft_valuation(
        db,
        element_in=int(element_in),
        element_out=int(element_out),
        free_transfers=int(squad.free_transfers),
        start_gw=int(target_gw),
    )
    # S3 Hit Cost Analysis chip payload
    hit_analysis: dict[str, Any] = {
        "hit_cost": valuation["hit_cost"],
        "gross_gain": valuation["gross_ev"],
        "net_ev": valuation["net_ev"],
        "recommendation": valuation["recommendation"],
        "chip_text": (
            f"Cost: -{valuation['hit_cost']} pts. Projected 3-week gain: +{valuation['gross_ev']} pts. Net EV: {valuation['net_ev']:+.1f}. Recommendation: {valuation['recommendation']}."
            if valuation["hit_cost"] > 0
            else f"Cost: 0 pts (free transfer). Projected 3-week gain: {valuation['gross_ev']:+.1f} pts. Net EV: {valuation['net_ev']:+.1f}. Recommendation: {valuation['recommendation']}."
        ),
    }
    # price / name enrichment
    try:
        from fpl_intelligence.prediction.live_provider import load_player_catalog

        cat = load_player_catalog()
        for key in ("element_in", "element_out"):
            pid = int(valuation[key])
            row = cat.get(pid, {})
            valuation[f"{key}_name"] = row.get("web_name") or f"Player {pid}"
            valuation[f"{key}_price"] = row.get("price")
    except Exception:
        pass
    return {
        "session_id": session_id,
        "status": "ok",
        "valuation": valuation,
        "hit_analysis": hit_analysis,
        "how_computed": valuation.get("how_computed"),
    }


@router.get("/shadow", include_in_schema=False)
async def transfers_shadow(
    response: Response,
    db: deps.GetDB,
    session_id: str = Query(..., description="FPL entry id (= session key)"),
    element_in: int = Query(..., gt=0),
    element_out: int = Query(..., gt=0),
) -> dict[str, Any]:
    """Shadow Squad: recalculate Alpha/xPTS/Captaincy against staged squad.

    v2.7.3-dual-state: reads the *effective* (local-preferred) squad as base.
    """
    response.headers["Cache-Control"] = "no-store"
    from fpl_intelligence.squad.service import SquadService
    from fpl_intelligence.transfers.shadow import build_shadow_squad, shadow_metrics

    squad = SquadService(session=db).get_effective_squad(session_id=session_id)
    if squad is None:
        return {"status": "no-squad", "note": "No squad saved for this session"}

    shadow_ids = build_shadow_squad(list(squad.player_ids or []), int(element_out), int(element_in))
    if shadow_ids is None:
        return {
            "status": "invalid",
            "note": "Staged transfer invalid: OUT not in squad or IN already owned.",
            "element_in": element_in,
            "element_out": element_out,
        }

    try:
        from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

        target_gw = await resolve_target_gameweek(db, fallback=int(squad.gameweek))
    except Exception:  # noqa: BLE001
        target_gw = int(squad.gameweek)

    metrics = shadow_metrics(db, squad, shadow_ids, int(target_gw))

    # Enrich with names for UI
    try:
        from fpl_intelligence.prediction.live_provider import load_player_catalog

        cat = load_player_catalog()
        metrics["staged_in_name"] = cat.get(int(element_in), {}).get("web_name") or f"Player {element_in}"
        metrics["staged_out_name"] = cat.get(int(element_out), {}).get("web_name") or f"Player {element_out}"
    except Exception:
        metrics["staged_in_name"] = f"Player {element_in}"
        metrics["staged_out_name"] = f"Player {element_out}"

    # Captaincy: run optimizer for both current and shadow to show delta
    captain_delta: dict[str, Any] | None = None
    try:
        from fpl_intelligence.api.deps import get_prediction_provider
        from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
        from fpl_intelligence.squad.models import SquadStateCreate

        provider = get_prediction_provider(db)  # type: ignore[arg-type]
        bridge = DecisionOptimizerBridge(provider=provider)
        # Current
        cur_report = bridge.generate_decisions(squad)
        cur_cap = cur_report.captain.player_id if cur_report.captain else squad.captain_id
        # Shadow
        shadow_squad = SquadStateCreate(
            player_ids=shadow_ids,
            captain_id=squad.captain_id if squad.captain_id != int(element_out) else int(element_in),
            vice_captain_id=squad.vice_captain_id if squad.vice_captain_id != int(element_out) else int(element_in),
            bank=float(squad.bank),
            free_transfers=max(0, int(squad.free_transfers) - (0 if int(squad.free_transfers) > 0 else 0)),
            chips_available=list(squad.chips_available or []),
            gameweek=int(target_gw),
            player_positions=squad.player_positions,
            player_prices=squad.player_prices,
            player_teams=squad.player_teams,
            session_id=session_id,
        )
        # Adjust prices dict for shadow
        if shadow_squad.player_prices and int(element_in) not in shadow_squad.player_prices:
            try:
                from fpl_intelligence.prediction.live_provider import load_player_catalog

                cat2 = load_player_catalog()
                shadow_squad.player_prices[int(element_in)] = float(cat2.get(int(element_in), {}).get("price") or 0.0)
            except Exception:
                pass
            shadow_squad.player_prices.pop(int(element_out), None)
        shad_report = bridge.generate_decisions(shadow_squad)
        shad_cap = shad_report.captain.player_id if shad_report.captain else shadow_squad.captain_id
        captain_delta = {
            "current_captain": cur_cap,
            "shadow_captain": shad_cap,
            "changed": cur_cap != shad_cap,
            "current_xpts": cur_report.captain.expected_points if cur_report.captain else None,
            "shadow_xpts": shad_report.captain.expected_points if shad_report.captain else None,
        }
    except Exception as exc:  # noqa: BLE001 — shadow captaincy is best-effort
        logger.debug("shadow captaincy failed: %s", exc)

    return {
        "session_id": session_id,
        "status": "ok",
        "staged": {"element_in": int(element_in), "element_out": int(element_out)},
        "shadow": metrics,
        "shadow_ids": shadow_ids,
        "captain_delta": captain_delta,
        "bank": float(squad.bank),
        "free_transfers": int(squad.free_transfers),
    }


class SaveLocalBody(BaseModel):
    session_id: str = Field(..., description="FPL entry id (= session key)")
    element_in: int = Field(..., gt=0)
    element_out: int = Field(..., gt=0)


@router.post("/save-local", include_in_schema=False)
async def save_local_squad(
    body: SaveLocalBody, db: deps.GetDB, response: Response
) -> dict[str, Any]:
    """v2.7.3-dual-state: persist staged transfer to ``local_squad``.

    No FPL fetch, no egress mask — pure local DB upsert. Returns the saved
    effective squad and invalidates the per-session decisions cache so the
    next ``GET /decisions`` / ``/targets`` reflects the new XI.
    """
    from fpl_intelligence.squad.models import SquadStateCreate
    from fpl_intelligence.squad.service import SquadService
    from fpl_intelligence.transfers.shadow import build_shadow_squad

    svc = SquadService(session=db)
    cur = svc.get_effective_squad(session_id=body.session_id)
    if cur is None:
        from fastapi import HTTPException as _HTTP

        raise _HTTP(status_code=404, detail="No squad saved for this session — import your team first.")
    shadow_ids = build_shadow_squad(list(cur.player_ids), int(body.element_out), int(body.element_in))
    if shadow_ids is None:
        from fastapi import HTTPException as _HTTP

        raise _HTTP(status_code=422, detail="Staged transfer invalid: OUT not in squad or IN already owned.")

    try:
        from fpl_intelligence.prediction.live_provider import load_player_catalog  # noqa: PLC0415

        catalog = load_player_catalog()
    except Exception:
        catalog = {}

    bank = float(cur.bank or 0.0)
    try:
        price_in = float(catalog.get(int(body.element_in), {}).get("price") or 0.0)
        price_out = float(catalog.get(int(body.element_out), {}).get("price") or 0.0)
        if not price_in:
            price_in = float((cur.player_prices or {}).get(int(body.element_in)) or 0.0)
        if not price_out:
            price_out = float((cur.player_prices or {}).get(int(body.element_out)) or 0.0)
        if price_in or price_out:
            bank = round(bank + price_out - price_in, 1)
    except Exception:
        pass

    captain_id = int(cur.captain_id)
    vice_id = int(cur.vice_captain_id)
    if captain_id == int(body.element_out):
        captain_id = int(body.element_in)
    if vice_id == int(body.element_out):
        vice_id = int(body.element_in)

    new_positions = dict(cur.player_positions or {})
    new_prices = dict(cur.player_prices or {})
    new_teams = dict(cur.player_teams or {})
    cat_row = catalog.get(int(body.element_in), {})
    if cat_row.get("position"):
        new_positions[int(body.element_in)] = int(cat_row["position"])
    new_positions.pop(int(body.element_out), None)
    if cat_row.get("price") is not None:
        new_prices[int(body.element_in)] = float(cat_row["price"])
    new_prices.pop(int(body.element_out), None)
    if cat_row.get("team") is not None:
        new_teams[int(body.element_in)] = int(cat_row["team"])
    new_teams.pop(int(body.element_out), None)

    payload = cur.model_copy(
        update={
            "player_ids": shadow_ids,
            "captain_id": captain_id,
            "vice_captain_id": vice_id,
            "bank": bank,
            "player_positions": new_positions or None,
            "player_prices": new_prices or None,
            "player_teams": new_teams or None,
            "session_id": body.session_id,
        }
    )
    saved = svc.set_local_squad(
        SquadStateCreate(**{k: v for k, v in payload.model_dump().items() if k != "updated_at"}),
        session_id=body.session_id,
    )
    try:
        from fpl_intelligence.api.routes.squad import _invalidate_decisions_cache  # noqa: PLC0415

        _invalidate_decisions_cache(body.session_id)
    except Exception:
        pass
    response.headers["Cache-Control"] = "no-store"
    return {
        "status": "ok",
        "session_id": body.session_id,
        "saved": saved.model_dump(mode="json"),
        "note": "Local squad saved — trajectory & Alpha now use this XI; league data unchanged.",
    }


# v2.7.2 — POST /execute deleted. App is planner-only; FPL is the execution layer.
