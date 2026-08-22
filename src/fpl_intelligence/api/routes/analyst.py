"""Phase 17.0 — AI Analyst summary endpoint.

Produces a 3-5 sentence plain-English summary of the week's decisions
(captain logic, transfer stance, risk flags) via the existing LLM router.
Falls back to a deterministic template when no LLM key is configured or the
provider fails — the response always arrives, and the model label tells the
user which path produced it.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from fpl_intelligence.api import deps
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider

router = APIRouter()

logger = logging.getLogger(__name__)


def _template_summary(report: dict[str, Any]) -> str:
    """Deterministic fallback when no LLM is available."""
    captain = report.get("captain") or {}
    transfers = report.get("transfer_plan") or {}
    chain = (report.get("meta") or {}).get("chain") or {}
    source_label = chain.get("source_label", "the prediction engine")

    c_name = _captain_name(report)
    c_pts = captain.get("expected_points")
    c_pts_str = f"{c_pts:.1f}" if c_pts is not None else "—"

    transfer_action = transfers.get("action_type", "roll")
    if transfer_action == "roll":
        transfer_line = "The engine recommends rolling your free transfer."
    elif transfer_action in ("Free Transfer", "Hit"):
        transfer_line = f"The engine recommends a {transfer_action.lower()}."
    else:
        transfer_line = "The engine recommends holding your squad."

    risk = captain.get("main_risk")
    risk_line = f" Main risk: {risk}." if risk else ""

    return (
        f"{c_name} is your captain this week, projected {c_pts_str} points, "
        f"based on {source_label}. "
        f"{transfer_line}{risk_line}"
    )


def _captain_name(report: dict[str, Any]) -> str:
    captain = report.get("captain") or {}
    cid = captain.get("player_id")
    players = report.get("players") or {}
    if cid is not None:
        p = players.get(str(cid)) or {}
        if p.get("web_name"):
            return p["web_name"]
    return "No captain"


def _build_prompt(report: dict[str, Any]) -> str:
    captain = report.get("captain") or {}
    transfers = report.get("transfer_plan") or {}
    chain = (report.get("meta") or {}).get("chain") or {}
    players = report.get("players") or {}

    c_name = _captain_name(report)
    c_pts = captain.get("expected_points")
    c_reason = captain.get("main_reason", "")
    c_risk = captain.get("main_risk", "")
    c_alts = captain.get("alternatives", [])[:2]

    transfer_action = transfers.get("action_type", "roll")
    transfer_reason = transfers.get("main_reason", "")

    source = chain.get("source_label", "prediction engine")
    quality = chain.get("data_quality", "")

    lines = [
        f"Captain: {c_name} (xPTS {c_pts:.1f})" if c_pts is not None else f"Captain: {c_name}",
        f"Captain reason: {c_reason}" if c_reason else "",
        f"Captain risk: {c_risk}" if c_risk else "",
    ]
    if c_alts:
        for alt in c_alts:
            ap = players.get(str(alt.get("player_id", "")), {})
            aname = ap.get("web_name", f"Player {alt.get('player_id')}")
            margin = alt.get("margin", 0)
            xpts = alt.get("expected_points", 0)
            lines.append(
                f"Alternative: {aname} (xPTS {xpts:.1f}, margin -{margin:.2f})"
            )

    lines.append(
        f"Transfer stance: {transfer_action} — {transfer_reason}"
        if transfer_reason
        else f"Transfer stance: {transfer_action}"
    )
    lines.append(
        f"Prediction source: {source} ({quality})"
        if quality
        else f"Prediction source: {source}"
    )

    body = "\n".join(lines)
    return (
        "You are an FPL analyst. Summarize the following week-ahead decisions "
        "in 3-5 plain-English sentences for a fantasy manager. Mention the captain "
        "logic, transfer stance, and any risk flags. Be specific and do not invent "
        "numbers.\n\n" + body
    )


@router.get("/analyst/summary")
async def analyst_summary(
    request: Request,
    response: Response,
    db: deps.GetDB,
    provider: Any = Depends(deps.get_llm_provider),  # noqa: B008
    session_id: str | None = Query(None),
) -> dict[str, Any]:
    """Return a plain-English summary of the squad's week-ahead decisions.

    Uses the configured LLM provider (GROQ/OPENROUTER/GEMINI) when available,
    otherwise falls back to a deterministic template. The ``model`` field
    discloses which path produced the text.
    """
    if not session_id:
        raise HTTPException(status_code=404, detail="No squad saved for this session")

    from fpl_intelligence.api.routes.squad import (  # noqa: PLC0415
        _resolve_chain_meta,
    )
    from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
    from fpl_intelligence.squad.service import SquadService

    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(status_code=404, detail="No squad saved for this session")

    bridge = DecisionOptimizerBridge(
        provider=deps.get_prediction_provider(db)
    )
    report = bridge.generate_decisions(squad)
    chain_meta = _resolve_chain_meta(deps.get_prediction_provider(db), report.gameweek)
    if chain_meta is not None:
        report.meta["chain"] = chain_meta

    report_dict = report.model_dump()

    use_live = os.getenv("FPL_API_USE_LIVE_LLM", "false").strip().lower() == "true"
    if not use_live or isinstance(provider, MockLLMProvider):
        summary = _template_summary(report_dict)
        model = "template-fallback"
    else:
        prompt = _build_prompt(report_dict)
        try:
            raw = await run_in_threadpool(provider.generate, prompt)
            summary = raw.strip() if raw and raw.strip() else _template_summary(report_dict)
            model = getattr(provider, "model", None) or getattr(provider, "model_id", None) or "llm"
        except Exception as exc:  # noqa: BLE001 - never fail the endpoint
            logger.warning("Analyst LLM failed (%s); using template.", exc)
            summary = _template_summary(report_dict)
            model = "template-fallback"

    return {
        "summary": summary,
        "model": model,
        "session_id": session_id,
        "gameweek": report.gameweek,
    }
