"""Phase 18.0 — AI Analyst summary endpoint.

Phase 21.1 rework: this is a READER. The daily cron pre-generates each
squad's brief; the Decisions card serves that cached text (or the personal
deterministic template instantly on a miss). No LLM call happens on-request,
so the card can never spin longer than the round-trip.

The provider-construction helpers stay exported because the assistant route
(allowed to generate inside the cron window) and the provider audit tests
build on them.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from fpl_intelligence.api import deps
from fpl_intelligence.live_intelligence.llm_settings import (
    LLMProviderName,
    load_llm_settings,
)
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider

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


@router.get("/analyst/summary")
async def analyst_summary(
    request: Request,
    response: Response,
    db: deps.GetDB,
    session_id: str | None = Query(None),
) -> dict[str, Any]:
    """Return a plain-English summary of the squad's week-ahead decisions.

    Phase 21.1 (T3): this endpoint is a READER. It serves the pre-generated
    daily brief (TL;DR + section extract) when one exists and otherwise falls
    back to the deterministic personal template instantly. No LLM call ever
    happens on-request, so the Decisions card never spins longer than the
    round-trip.
    """
    if not session_id:
        raise HTTPException(status_code=404, detail="No squad saved for this session")

    from fpl_intelligence.api.routes.assistant import load_pregenerated_brief
    from fpl_intelligence.api.routes.squad import (  # noqa: PLC0415
        _resolve_chain_meta,
    )
    from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
    from fpl_intelligence.squad.service import SquadService
    from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek  # noqa: PLC0415

    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    gameweek = await resolve_target_gameweek(db, fallback=int(squad.gameweek))

    # --- preferred path: the cron's pre-generated brief -----------------------
    brief = load_pregenerated_brief(db, session_id, gameweek)
    if brief is None:
        # Fall back to the newest stored brief even when its gameweek differs —
        # an honest slightly-stale analyst note beats a generic template.
        brief = load_pregenerated_brief(db, session_id)

    bridge = DecisionOptimizerBridge(provider=deps.get_prediction_provider(db))
    report = bridge.generate_decisions(squad)
    chain_meta = _resolve_chain_meta(deps.get_prediction_provider(db), report.gameweek)
    if chain_meta is not None:
        report.meta["chain"] = chain_meta

    report_dict = report.model_dump()
    template_summary = _template_summary(report_dict)

    if brief:
        summary = _summary_from_brief(brief) or template_summary
        model = brief.get("model") or "pre-generated"
        generated_at = brief.get("generated_at")
        source_label = f"pre-generated{f' · {model}' if model and model != 'pre-generated' else ''}"
        return {
            "summary": summary,
            "model": source_label,
            "session_id": session_id,
            "gameweek": int(brief.get("gameweek") or report.gameweek),
            "cached": True,
            "generated_at": generated_at,
        }

    return {
        "summary": template_summary,
        "model": "template-fallback",
        "session_id": session_id,
        "gameweek": report.gameweek,
        "cached": False,
        "generated_at": None,
    }


def _summary_from_brief(brief: dict[str, Any]) -> str | None:
    """Compose the analyst paragraph out of the persisted brief.

    Prefers the TL;DR action lines; appends the captain section body trimmed
    to keep the card readable. Returns ``None`` when the brief carries nothing
    usable so callers fall back to the live template.
    """
    actions = [
        str(action.get("text", "")).strip()
        for action in (brief.get("tldr") or [])
        if isinstance(action, dict)
    ]
    actions = [a for a in actions if a]
    sections = brief.get("sections") or {}
    captain_body = ""
    for key in ("CAPTAIN", "captain"):
        body = sections.get(key)
        if isinstance(body, str) and body.strip():
            captain_body = body.strip()
            break
    parts: list[str] = []
    if actions:
        parts.append(" ".join(actions))
    if captain_body:
        trimmed = captain_body if len(captain_body) <= 400 else captain_body[:397] + "…"
        parts.append(trimmed)
    if not parts:
        return None
    gw = brief.get("gameweek")
    prefix = f"GW{gw}: " if gw else ""
    return prefix + " ".join(parts)


def _resolve_model_label(provider: Any) -> str | None:
    """Best-effort human-readable "provider/model" label for the analyst card."""
    if provider is None:
        return None
    name = getattr(provider, "provider_name", None)
    model = getattr(provider, "model_name", None)
    if name and model:
        return f"{name}/{model}"
    return name or model or getattr(provider, "model", None) or None
