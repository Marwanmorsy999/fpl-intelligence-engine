"""Phase 18.0 — AI Analyst summary endpoint.

Produces a 3-5 sentence plain-English summary of the week's decisions
(captain logic, transfer stance, risk flags). When at least one real LLM key
is configured (GROQ/OPENROUTER/GEMINI) the summary is produced by the real
provider chain in that priority order; the card labels the model that answered.
Falls back to a deterministic template ONLY on a real failure (all providers
exhausted) — never just because keys exist.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from fpl_intelligence.api import deps
from fpl_intelligence.live_intelligence.llm_settings import (
    LLMProviderName,
    load_llm_settings,
)
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider
from fpl_intelligence.live_intelligence.prompts import ANALYST_SUMMARY, LLMPrompt

router = APIRouter()

logger = logging.getLogger(__name__)

#: Per-provider attempt budget for the analyst (P3): 8s timeout each, short
#: pacing between fallback attempts so the card answers inside serverless limits.
_ANALYST_LLM_TIMEOUT_SECONDS = 8.0
_ANALYST_LLM_MIN_INTERVAL_SECONDS = 2.0


def _build_real_provider() -> Any:
    """Build a provider that tries GROQ -> OPENROUTER -> GEMINI in order.

    Reads the configured ``llm_provider`` first, then falls back through the
    remaining providers that have a key. Returns a MockLLMProvider only when
    no real key exists at all. The response cache is in-memory: Vercel's
    read-only filesystem cannot host the SQLite cache, and a construction
    failure there would silently degrade every answer to template-fallback.
    """
    try:
        settings = load_llm_settings(
            llm_timeout_seconds=_ANALYST_LLM_TIMEOUT_SECONDS,
            llm_min_seconds_between_calls=_ANALYST_LLM_MIN_INTERVAL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load LLM settings: %s", exc)
        return MockLLMProvider()

    configured = settings.configured_providers()
    if not configured:
        return MockLLMProvider()

    from fpl_intelligence.live_intelligence.llm_providers import ProviderFactory
    from fpl_intelligence.live_intelligence.provider_router import ProviderRouter
    from fpl_intelligence.live_intelligence.response_cache import InMemoryResponseCache

    factory = ProviderFactory(settings)
    # Prefer the configured provider first, then the rest in priority order.
    priority = [LLMProviderName.GROQ, LLMProviderName.OPENROUTER, LLMProviderName.GEMINI]
    primary = settings.provider if settings.provider.is_real else None
    fallback_order: list[LLMProviderName] = []
    if primary is not None and primary not in fallback_order:
        fallback_order.append(primary)
    for p in priority:
        if p in configured and p not in fallback_order:
            fallback_order.append(p)
    fallback_order = [p for p in fallback_order if p in configured]

    if not fallback_order:
        return MockLLMProvider()

    return ProviderRouter(
        factory=factory,
        fallback_order=fallback_order,
        cache=InMemoryResponseCache(),
    )


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


def _build_prompt(report: dict[str, Any]) -> LLMPrompt:
    """Render the registered analyst template with this report's facts."""
    return ANALYST_SUMMARY.render(
        context={"gameweek": report.get("gameweek")},
        raw_text=_render_report_text(report),
    )


def _render_report_text(report: dict[str, Any]) -> str:
    """Render the decision report as the prompt's user text."""
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
            lines.append(f"Alternative: {aname} (xPTS {xpts:.1f}, margin -{margin:.2f})")

    lines.append(
        f"Transfer stance: {transfer_action} — {transfer_reason}"
        if transfer_reason
        else f"Transfer stance: {transfer_action}"
    )
    lines.append(
        f"Prediction source: {source} ({quality})" if quality else f"Prediction source: {source}"
    )

    return "\n".join(lines)


@router.get("/analyst/summary")
async def analyst_summary(
    request: Request,
    response: Response,
    db: deps.GetDB,
    session_id: str | None = Query(None),
) -> dict[str, Any]:
    """Return a plain-English summary of the squad's week-ahead decisions.

    Uses a real LLM chain (GROQ -> OPENROUTER -> GEMINI) whenever any key is
    configured; template fallback only on a real failure. ``model`` discloses
    which provider/model actually produced the text.
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

    bridge = DecisionOptimizerBridge(provider=deps.get_prediction_provider(db))
    report = bridge.generate_decisions(squad)
    chain_meta = _resolve_chain_meta(deps.get_prediction_provider(db), report.gameweek)
    if chain_meta is not None:
        report.meta["chain"] = chain_meta

    report_dict = report.model_dump()

    # P3/E5: use a real LLM when any key exists; template fallback ONLY on a
    # real failure. The card always labels the model/chain that answered.
    llm = _build_real_provider()

    if isinstance(llm, MockLLMProvider):
        summary = _template_summary(report_dict)
        model = "template-fallback"
    else:
        prompt = _build_prompt(report_dict)
        try:
            raw = await run_in_threadpool(llm.complete, prompt)
            if raw and raw.text and raw.text.strip():
                summary = raw.text.strip()
            else:
                summary = _template_summary(report_dict)
            # Label from the provider that ACTUALLY answered (fallback-aware).
            resp_provider = getattr(raw, "provider_name", None)
            resp_model = getattr(raw, "model_name", None)
            if resp_provider and resp_model:
                model = f"{resp_provider}/{resp_model}"
            else:
                model = _resolve_model_label(llm) or "llm"
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


def _resolve_model_label(provider: Any) -> str | None:
    """Best-effort human-readable "provider/model" label for the analyst card."""
    if provider is None:
        return None
    name = getattr(provider, "provider_name", None)
    model = getattr(provider, "model_name", None)
    if name and model:
        return f"{name}/{model}"
    return name or model or getattr(provider, "model", None) or None
