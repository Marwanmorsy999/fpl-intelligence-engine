"""Phase 25 Gate 0 (T1) — transfer intelligence API + Phase 27 Shadow Squad.

``GET /api/v1/transfers/ledger?entry_id=`` returns the materialized ledger
with horizon EV per row and the honest source label. ``GET
/api/v1/transfers/detected?session_id=`` powers the on-sync banner when a
snapshot change implies a transfer.

Phase 27 Gate 0 adds:
* ``GET /shadow`` — staged transfer valuation + shadow squad metrics
                 (labelled "STAGED - Not yet pushed to FPL").
* ``GET /valuation`` — FT Valuation over next 3 GWs vs keeping player.
* ``POST /execute`` — direct push via egress mask with clipboard fallback.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Query, Response
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
# Phase 27 Gate 0 — Shadow Squad (T1) + FT Valuation (S3) + Direct Push (T2)
# --------------------------------------------------------------------------- #


class ShadowRequest(BaseModel):
    session_id: str = Field(..., description="FPL entry id / session key")
    element_in: int = Field(..., gt=0)
    element_out: int = Field(..., gt=0)


class ExecuteRequest(BaseModel):
    session_id: str = Field(..., description="FPL entry id / session key")
    element_in: int = Field(..., gt=0)
    element_out: int = Field(..., gt=0)
    # Optional session cookie/CSRF forwarded from the Apps Script egress mask.
    # When absent, the endpoint returns the clipboard fallback honestly.
    fpl_session_cookie: str | None = Field(None, description="FPL session cookie (optional)")
    csrf_token: str | None = Field(None, description="FPL CSRF token (optional)")


@router.get("/valuation", include_in_schema=False)
async def transfers_valuation(
    response: Response,
    db: deps.GetDB,
    session_id: str = Query(..., description="FPL entry id (= session key)"),
    element_in: int = Query(..., gt=0),
    element_out: int = Query(..., gt=0),
) -> dict[str, Any]:
    """FT Valuation: projected net EV of staged transfer over next 3 GWs."""
    response.headers["Cache-Control"] = "no-store"
    from fpl_intelligence.squad.service import SquadService
    from fpl_intelligence.transfers.shadow import compute_ft_valuation

    squad = SquadService(session=db).get_squad(session_id=session_id)
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
    """Shadow Squad: recalculate Alpha/xPTS/Captaincy against staged squad."""
    response.headers["Cache-Control"] = "no-store"
    from fpl_intelligence.squad.service import SquadService
    from fpl_intelligence.transfers.shadow import build_shadow_squad, shadow_metrics

    squad = SquadService(session=db).get_squad(session_id=session_id)
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


@router.post("/execute", include_in_schema=False)
async def transfers_execute(
    response: Response,
    db: deps.GetDB,
    body: ExecuteRequest = Body(...),  # noqa: B008
) -> dict[str, Any]:
    """Direct push to FPL via egress mask, with clipboard fallback.

    Attempts POST /api/transfers/ through the Apps Script mask. When the mask
    is unavailable or the session/CSRF is missing, returns an honest fallback
    payload: clipboard text + FPL URL so the UI can offer "Open FPL to Confirm".
    On success, invalidates the decisions cache and snapshots the new squad.
    """
    response.headers["Cache-Control"] = "no-store"
    from fpl_intelligence.squad.service import SquadService
    from fpl_intelligence.transfers.shadow import build_shadow_squad

    squad = SquadService(session=db).get_squad(session_id=body.session_id)
    if squad is None:
        return {"status": "no-squad", "note": "No squad saved for this session"}

    # Validate staged ids
    shadow_ids = build_shadow_squad(list(squad.player_ids or []), int(body.element_out), int(body.element_in))
    if shadow_ids is None:
        return {"status": "invalid", "note": "OUT not in squad or IN already owned."}

    # Enrich names for clipboard
    try:
        from fpl_intelligence.prediction.live_provider import load_player_catalog

        cat = load_player_catalog()
        name_in = cat.get(int(body.element_in), {}).get("web_name") or f"Player {body.element_in}"
        name_out = cat.get(int(body.element_out), {}).get("web_name") or f"Player {body.element_out}"
    except Exception:
        name_in = f"Player {body.element_in}"
        name_out = f"Player {body.element_out}"

    clipboard = f"IN: {name_in}, OUT: {name_out}"
    fpl_url = "https://fantasy.premierleague.com/transfers"

    # Attempt egress POST when cookie/CSRF forwarded and mask configured
    attempted = False
    success = False
    error_note = ""
    if body.fpl_session_cookie and body.csrf_token:
        attempted = True
        try:
            from fpl_intelligence.config import get_settings

            settings = get_settings()
            base = settings.fpl_base_url.rstrip("/")
            # Apps Script mask POST contract: `${FPL_PROXY_URL}?url=<encoded>&method=POST&csrf=<token>`
            # The script forwards cookies + CSRF header to FPL.
            # When FPL_PROXY_URL not set, this branch is skipped and we fall through.
            import os
            from urllib.parse import quote

            proxy = os.getenv("FPL_PROXY_URL", "").strip() or getattr(settings, "fpl_proxy_url", "")  # type: ignore[attr-defined]
            if proxy:
                import httpx

                target = f"{base}/api/transfers/"
                # Mask POST shape — try JSON body with entry + transfers array
                payload = {
                    "entry": int(body.session_id) if str(body.session_id).isdigit() else body.session_id,
                    "event": int(squad.gameweek),
                    "transfers": [{"element_in": int(body.element_in), "element_out": int(body.element_out)}],
                }
                # App Script expects `url` param + method override; we POST to the mask itself.
                mask_url = proxy.split("?url=")[0].rstrip("?&")
                async with httpx.AsyncClient(timeout=6.0) as client:
                    resp = await client.post(
                        mask_url,
                        params={
                            "url": target,
                            "method": "POST",
                            "csrf": body.csrf_token,
                        },
                        headers={
                            "Cookie": body.fpl_session_cookie,
                            "X-CSRFToken": body.csrf_token,
                            "Referer": "https://fantasy.premierleague.com/",
                        },
                        json=payload,
                    )
                    if resp.status_code in (200, 201, 202):
                        success = True
                    else:
                        error_note = f"mask POST {resp.status_code}: {resp.text[:200]}"
            else:
                error_note = "FPL_PROXY_URL not configured — clipboard fallback."
        except Exception as exc:  # noqa: BLE001 — honest fallback
            error_note = f"{type(exc).__name__}: {exc}"

    if success:
        # Invalidate cache, capture new snapshot as staged truth, return success
        try:
            from fpl_intelligence.api.routes.squad import _invalidate_decisions_cache

            _invalidate_decisions_cache(str(body.session_id))
        except Exception:
            pass
        try:
            from fpl_intelligence.transfers.service import capture_snapshot

            capture_snapshot(db, str(body.session_id), shadow_ids, int(squad.gameweek), float(squad.bank))
        except Exception:
            pass
        return {
            "status": "executed",
            "message": "Transfer Executed on FPL",
            "clipboard": clipboard,
            "fpl_url": fpl_url,
            "staged": {"element_in": int(body.element_in), "element_out": int(body.element_out)},
            "shadow_ids": shadow_ids,
        }

    # Honest fallback — caller shows "Open FPL to Confirm" + copies clipboard
    return {
        "status": "fallback",
        "message": "Open FPL to Confirm",
        "clipboard": clipboard,
        "fpl_url": fpl_url,
        "staged": {"element_in": int(body.element_in), "element_out": int(body.element_out)},
        "attempted": attempted,
        "error": error_note or "Mask write unavailable — use clipboard fallback.",
        "note": "If mask write failed/blocked, copy the IN/OUT line and confirm on the FPL site.",
    }
