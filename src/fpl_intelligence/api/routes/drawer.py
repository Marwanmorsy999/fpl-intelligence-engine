"""Phase 20.0 ??? deep-analysis player drawer endpoint.

``GET /api/v1/player/{player_id}/drawer?session_id=`` bundles everything the
frontend drawer shows in one call:

* last-5 gameweek form bars (materialized ``ingested_history``),
* Understat xG/xA per 90 (matched players only, offline snapshot),
* minutes played, selected-by %, price change (materialized ``element_facts``),
* xPTS + breakdown from the prediction chain,
* next-5 fixture strip (materialized ``fixtures_cache``),
* BBC news flags when any headline matched (materialized ``news_cache``).

Phase 20.1: every input is read from indexed tables written by the daily
06:10 materialize cron ??? ZERO live network fetches in the request path. This
is the fix for the production 504 (the old implementation fetched bootstrap +
element-summary live per request and hung until timeout behind blocked FPL).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.api.routes.fixtures import _team_names, load_fixtures
from fpl_intelligence.data_providers.bbc_news import (
    NEWS_KEYWORDS,
    build_aliases,
    match_headlines,
)
from fpl_intelligence.fixtures.scanner import (
    NEUTRAL_FDR,
    average_fdr,
    next_gameweeks,
    parse_fixtures,
    player_run,
)
from fpl_intelligence.materialize.service import NEWS_MAX_AGE_SECONDS
from fpl_intelligence.squad.service import SquadService
from fpl_intelligence.sync.materialized_models import ElementFactDB
from fpl_intelligence.sync.models import IngestedGameweekDB

router = APIRouter(prefix="/player", tags=["player"])
logger = logging.getLogger(__name__)

GetDB = deps.GetDB

FORM_GWS = 5
HORIZON_GWS = 5


def _form_bars_from_history(db: Session, player_id: int) -> list[dict[str, Any]]:
    """Last-5 finished gameweeks from the materialized results table."""
    rows = db.execute(
        select(
            IngestedGameweekDB.gameweek,
            IngestedGameweekDB.total_points,
            IngestedGameweekDB.minutes,
        )
        .where(IngestedGameweekDB.element_id == int(player_id))
        .order_by(IngestedGameweekDB.gameweek.desc())
        .limit(FORM_GWS)
    ).all()
    return [
        {"gw": gw, "points": points, "minutes": minutes}
        for gw, points, minutes in sorted(rows)
    ]


@router.get("/{player_id}/drawer")
async def player_drawer(
    player_id: int,
    db: GetDB,
    response: Response,
    session_id: str | None = Query(None, description="Per-user session key. Required."),
    gw: int | None = Query(
        None, description="Gameweek override (defaults to the saved squad's GW)."
    ),
) -> dict[str, Any]:
    """Deep-analysis payload for one squad player."""
    if not session_id:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None or player_id not in (squad.player_ids or []):
        raise HTTPException(status_code=404, detail="Player not in this squad")
    response.headers["Cache-Control"] = "no-store"
    target_gw = int(gw) if gw else int(squad.gameweek)

    # --- identity ------------------------------------------------------------
    row: ElementFactDB | None = db.get(ElementFactDB, int(player_id))
    from fpl_intelligence.db.models import Player  # noqa: PLC0415

    prow: Player | None = db.scalar(select(Player).where(Player.fpl_element_id == player_id))
    web_name = (
        (prow.web_name if prow else None)
        or (row.web_name if row else None)
        or f"Player {player_id}"
    )
    first_name = prow.first_name if prow else ""
    second_name = prow.second_name if prow else ""

    # --- fixtures -------------------------------------------------------------
    fixture_runs: list[dict[str, Any]] = []
    avg_fdr = NEUTRAL_FDR
    team_names = _team_names(db)
    rows = parse_fixtures(await load_fixtures(db))
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
        runs = [r for r in player_run(team, rows_by_gw, horizon, team_names=team_names)]
        fixture_runs = [r.__dict__ for r in runs]
        real = [r for r in runs if r.opponent_id != 0]
        if real:
            avg_fdr = round(average_fdr(real), 2)

    # --- prediction chain xPTS + breakdown ------------------------------------
    # Phase 20.1: the provider serves the materialized table first (fast path);
    # the inline chain is only a fallback while the cron has never run.
    provider = deps.get_prediction_provider(db)
    expected_points = None
    breakdown = None
    prediction_source = None
    data_quality = None
    minutes_estimate = None
    start_prob = None
    xg = xa = None

    def _predict() -> Any:
        return provider.get_squad_predictions([player_id], [target_gw])

    try:
        preds = await run_in_threadpool(_predict)
        pred = (preds.get(target_gw) or {}).get(player_id)
        if pred is not None:
            expected_points = round(pred.expected_points, 2)
            prediction_source = getattr(pred, "source", None)
            data_quality = getattr(pred, "data_quality", None)
            if pred.expected_minutes is not None:
                minutes_estimate = round(float(pred.expected_minutes), 1)
            if pred.start_probability is not None:
                start_prob = round(float(pred.start_probability), 3)
            raw_breakdown = getattr(pred, "breakdown", None)
            if isinstance(raw_breakdown, dict):
                breakdown = {k: round(float(v), 2) for k, v in raw_breakdown.items()}
    except Exception as exc:  # noqa: BLE001 ??? xPTS is best-effort
        logger.warning("drawer predictions failed for %s: %s", player_id, exc)

    # --- Understat xG/xA (matched players only; OFFLINE snapshot, no network) --
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

    # --- element facts + last-5 form (both materialized) -----------------------
    minutes_played = selected_by = cost_change = status = None
    if row is not None:
        minutes_played = row.minutes
        selected_by = row.selected_by_percent
        cost_change = row.cost_change_event
        status = row.status
    if not selected_by:
        # Phase 22 (D1): seed-catalog fallback so ownership renders even while
        # element_facts is still cold early in the season.
        try:
            from fpl_intelligence.prediction.live_provider import load_player_catalog

            seed_row = load_player_catalog().get(int(player_id))
            if seed_row and seed_row.get("selected_by_percent"):
                selected_by = str(seed_row["selected_by_percent"])
        except Exception as exc:  # noqa: BLE001 — enrichment only
            logger.debug("drawer ownership fallback failed: %s", exc)

    form_bars = _form_bars_from_history(db, player_id)

    # --- news flags (materialized cache) ---------------------------------------
    news_flag = None
    from fpl_intelligence.api.routes.news import cached_items_from_db  # noqa: PLC0415

    items, fetched_at = cached_items_from_db(db, max_age_seconds=NEWS_MAX_AGE_SECONDS)
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

    generated_at = (
        fetched_at.isoformat()
        if fetched_at is not None
        else datetime.now(UTC).isoformat()
    )
    return {
        "session_id": session_id,
        "gameweek": target_gw,
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
        "generated_at": generated_at,
    }
