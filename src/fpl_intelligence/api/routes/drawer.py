"""Phase 20.0 — deep-analysis player drawer endpoint.

``GET /api/v1/player/{player_id}/drawer?session_id=`` bundles everything the
frontend drawer shows in one call:

* last-5 gameweek form bars (official ``element-summary`` history),
* Understat xG/xA per 90 (matched players only),
* minutes played, selected-by %, price change,
* xPTS + breakdown from the prediction chain,
* next-5 fixture strip,
* BBC news flags when any headline matched.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from fpl_intelligence.api import deps
from fpl_intelligence.api.routes.fixtures import load_fixtures
from fpl_intelligence.api.routes.news import _cached_items as cached_news_items
from fpl_intelligence.config import get_settings
from fpl_intelligence.data_providers.bbc_news import (
    NEWS_KEYWORDS,
    build_aliases,
    match_headlines,
)
from fpl_intelligence.data_providers.fpl_egress import (
    FplEgressChain,
    validate_bootstrap_payload,
)
from fpl_intelligence.db.models import Player
from fpl_intelligence.fixtures.scanner import (
    NEUTRAL_FDR,
    average_fdr,
    next_gameweeks,
    parse_fixtures,
    player_run,
)
from fpl_intelligence.prediction.live_provider import SOURCE_PROXY
from fpl_intelligence.squad.service import SquadService

router = APIRouter(prefix="/player", tags=["player"])
logger = logging.getLogger(__name__)

GetDB = deps.GetDB

FORM_GWS = 5
HORIZON_GWS = 5

_bootstrap_lock = threading.Lock()
_bootstrap_cache: tuple[float, dict[str, Any]] | None = None


async def _bootstrap_element(player_id: int) -> dict[str, Any] | None:
    """The bootstrap element row for one player (cache-first, 10 min TTL)."""
    global _bootstrap_cache
    ttl = 600.0
    with _bootstrap_lock:
        if _bootstrap_cache is not None and time.monotonic() - _bootstrap_cache[0] < ttl:
            elements = _bootstrap_cache[1].get("elements") or []
        else:
            settings = get_settings()
            egress = FplEgressChain(
                settings.fpl_base_url,
                timeout=settings.egress_strategy_timeout,
                cache_ttl=ttl,
            )
            try:
                payload = await egress.fetch(
                    "/api/bootstrap-static/", validator=validate_bootstrap_payload
                )
            except Exception as exc:  # noqa: BLE001 — drawer degrades gracefully
                logger.warning("bootstrap fetch failed in drawer: %s", exc)
                return None
            with _bootstrap_lock:
                _bootstrap_cache = (time.monotonic(), payload)
            elements = payload.get("elements") or []
    for el in elements:
        if isinstance(el, dict) and el.get("id") == player_id:
            return el
    return None


async def _element_summary_history(player_id: int) -> list[dict[str, Any]]:
    """Last finished gameweeks from the official element-summary endpoint."""
    settings = get_settings()
    egress = FplEgressChain(
        settings.fpl_base_url,
        timeout=min(6.0, settings.egress_strategy_timeout),
        cache_ttl=1800,
    )
    try:
        payload = await egress.fetch(f"/api/element-summary/{player_id}/")
    except Exception as exc:  # noqa: BLE001 — form bars are best-effort
        logger.warning("element-summary failed for %s: %s", player_id, exc)
        return []
    history = payload.get("history") or []
    return [h for h in history if isinstance(h, dict) and h.get("finished", True)]


@router.get("/{player_id}/drawer")
async def player_drawer(
    player_id: int,
    db: GetDB,
    response: Response,
    session_id: str | None = Query(None, description="Per-user session key. Required."),
) -> dict[str, Any]:
    """Deep-analysis payload for one squad player."""
    if not session_id:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None or player_id not in (squad.player_ids or []):
        raise HTTPException(status_code=404, detail="Player not in this squad")
    response.headers["Cache-Control"] = "no-store"

    # --- identity ------------------------------------------------------------
    row: Player | None = db.scalar(select(Player).where(Player.fpl_element_id == player_id))
    web_name = row.web_name if row else f"Player {player_id}"
    first_name = row.first_name if row else ""
    second_name = row.second_name if row else ""

    # --- fixtures -------------------------------------------------------------
    fixture_runs: list[dict[str, Any]] = []
    avg_fdr = NEUTRAL_FDR
    rows = parse_fixtures(await load_fixtures())
    if rows:
        current = max(
            min((r.event for r in rows if not r.finished), default=squad.gameweek),
            squad.gameweek,
        )
        horizon = next_gameweeks(rows, current, HORIZON_GWS)
        rows_by_gw: dict[int, list[Any]] = {}
        for r in rows:
            rows_by_gw.setdefault(r.event, []).append(r)
        team = (squad.player_teams or {}).get(player_id)
        runs = [r for r in player_run(team, rows_by_gw, horizon)]
        fixture_runs = [r.__dict__ for r in runs]
        real = [r for r in runs if r.opponent_id != 0]
        if real:
            avg_fdr = round(average_fdr(real), 2)

    # --- prediction chain xPTS + breakdown ------------------------------------
    # The provider chain does synchronous network I/O; offload it so the
    # event loop keeps serving other requests while it runs.
    provider = deps.get_prediction_provider(db)
    expected_points = None
    breakdown = None
    prediction_source = None
    data_quality = None
    minutes_estimate = None
    start_prob = None
    xg = xa = None

    def _predict() -> Any:
        return provider.get_squad_predictions([player_id], [squad.gameweek])

    try:
        preds = await run_in_threadpool(_predict)
        pred = (preds.get(squad.gameweek) or {}).get(player_id)
        if pred is not None:
            expected_points = round(pred.expected_points, 2)
            prediction_source = getattr(pred, "source", None)
            data_quality = getattr(pred, "data_quality", None)
            if pred.expected_minutes is not None:
                minutes_estimate = round(float(pred.expected_minutes), 1)
            if pred.start_probability is not None:
                start_prob = round(float(pred.start_probability), 3)
            raw_breakdown = getattr(pred, "breakdown", None)
            if prediction_source == SOURCE_PROXY and isinstance(raw_breakdown, dict):
                breakdown = {k: round(float(v), 2) for k, v in raw_breakdown.items()}
    except Exception as exc:  # noqa: BLE001 — xPTS is best-effort
        logger.warning("drawer predictions failed for %s: %s", player_id, exc)

    # --- Understat xG/xA (matched players only) -------------------------------
    index_getter = getattr(provider, "understat_index", None)
    if callable(index_getter) and web_name:
        try:
            uindex = await run_in_threadpool(index_getter) or {}
            from fpl_intelligence.data_providers.understat import (  # noqa: PLC0415
                UnderstatConnector,
                build_stats_from_row,
            )

            urow = UnderstatConnector.match_player(uindex, web_name)
            if urow is not None:
                stats = build_stats_from_row(urow)
                xg = round(float(stats.xg_per_90), 2)
                xa = round(float(stats.xa_per_90), 2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("drawer understat failed for %s: %s", player_id, exc)

    # --- bootstrap facts + last-5 form ----------------------------------------
    element = await _bootstrap_element(player_id)
    minutes_played = selected_by = cost_change = status = None
    if element is not None:
        minutes_played = element.get("minutes")
        selected_by = element.get("selected_by_percent")
        cost_change = element.get("cost_change_event")
        status = element.get("status")

    form_history = await _element_summary_history(player_id)
    form_bars = [
        {"gw": h.get("round"), "points": h.get("total_points"), "minutes": h.get("minutes")}
        for h in form_history[-FORM_GWS:]
    ]

    # --- news flags -----------------------------------------------------------
    news_flag = None
    items = await cached_news_items()
    if items and web_name:
        flags = match_headlines(
            items,
            [(player_id, web_name, first_name, second_name)],
            NEWS_KEYWORDS,
        )
        hit = flags.get(str(player_id))
        if hit is not None:
            news_flag = hit

    aliases = sorted(build_aliases(web_name, first_name, second_name))

    return {
        "session_id": session_id,
        "gameweek": squad.gameweek,
        "player": {
            "id": player_id,
            "web_name": web_name,
            "full_name": (f"{first_name} {second_name}").strip(),
            "team": (squad.player_teams or {}).get(player_id),
            "position": (squad.player_positions or {}).get(player_id),
            "price": (squad.player_prices or {}).get(player_id),
            "status": status,
            "minutes_played": minutes_played,
            "selected_by_percent": selected_by,
            "cost_change_event": cost_change,
        },
        "expected_points": expected_points,
        "prediction_source": prediction_source,
        "data_quality": data_quality,
        "xpts_breakdown": breakdown,
        "xg_per_90": xg,
        "xa_per_90": xa,
        "minutes_estimate": minutes_estimate,
        "start_prob": start_prob,
        "form_bars": form_bars,
        "fixture_runs": fixture_runs,
        "avg_fdr": avg_fdr,
        "news_flags": news_flag,
        "aliases": aliases,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
