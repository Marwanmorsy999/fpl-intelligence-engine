"""Phase 24 Gate 0 M3 — head-to-head compare endpoint.

GET /api/v1/compare?player_a=&player_b=&session_id=&gw=
Returns side-by-side cards with diff highlight metadata.
"""
# ruff: noqa: E501,F401,SIM105,SIM115,B009,I001,F841
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from fpl_intelligence.api import deps
from fpl_intelligence.api.routes.drawer import (
    _form_bars_from_history,
    _load_element_fact_safe,
    _load_prediction_materialized,
)
from fpl_intelligence.api.routes.fixtures import _team_names, load_fixtures
from fpl_intelligence.db.models import Player
from fpl_intelligence.fixtures.scanner import (
    NEUTRAL_FDR,
    average_fdr,
    next_gameweeks,
    parse_fixtures,
    player_run,
)
from fpl_intelligence.squad.service import SquadService

router = APIRouter(prefix="/compare", tags=["compare"])
logger = logging.getLogger(__name__)
GetDB = deps.GetDB

HORIZON_GWS = 5

def _set_piece_for(player_id: int, team_id: int | None) -> dict[str, Any]:
    try:
        from fpl_intelligence.set_pieces.service import set_piece_flags  # noqa: PLC0415

        return set_piece_flags(int(player_id), team_id)
    except Exception:
        if team_id is None:
            return {"penalty": False, "corners": False, "free_kicks": False, "unknown": True}
        return {"penalty": False, "corners": False, "free_kicks": False, "unknown": True}

def _player_payload(
    db,
    player_id: int,
    gameweek: int,
) -> dict[str, Any]:
    # resolve Player row for names
    prow: Player | None = None
    try:
        prow = db.scalar(select(Player).where(Player.fpl_element_id == player_id))
        if prow is None:
            prow = db.get(Player, player_id)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        prow = None

    web_name = (prow.web_name if prow else None) or f"Player {player_id}"
    first_name = prow.first_name if prow else ""
    second_name = prow.second_name if prow else ""

    # element fact
    row = _load_element_fact_safe(db, int(player_id))
    team_id = getattr(row, "team_id", None) if row else None
    price = None
    selected_by = getattr(row, "selected_by_percent", None) if row else None
    # price fallback via squad catalog?
    if price is None and row is not None and getattr(row, "now_cost", None) is not None:
        try:
            price = float(getattr(row, "now_cost")) / 10.0
        except Exception:
            price = None
    if price is None:
        try:
            from fpl_intelligence.prediction.live_provider import load_player_catalog
            cat = load_player_catalog().get(int(player_id))
            if cat and cat.get("price"):
                price = float(cat["price"])
        except Exception:
            pass
    if price is None:
        price = 0.0
    # team fallback from catalog
    if team_id is None:
        try:
            from fpl_intelligence.prediction.live_provider import load_player_catalog
            cat2 = load_player_catalog().get(int(player_id))
            if cat2 and cat2.get("team"):
                team_id = int(cat2["team"])
        except Exception:
            pass

    # position
    position = getattr(prow, "position_code", None) if prow else None
    if position is None:
        try:
            from fpl_intelligence.prediction.live_provider import load_player_catalog
            cat3 = load_player_catalog().get(int(player_id))
            if cat3 and cat3.get("position"):
                position = int(cat3["position"])
        except Exception:
            pass

    # team short
    team_short = None
    try:
        from fpl_intelligence.prediction.live_provider import load_player_catalog
        cat4 = load_player_catalog().get(int(player_id))
        if cat4 and cat4.get("team_short"):
            team_short = str(cat4["team_short"])
    except Exception:
        pass
    if not team_short and team_id is not None:
        try:
            names = _team_names(db)
            # _team_names returns id->display name, not short, but use it
            team_short = names.get(int(team_id), str(team_id))[:3].upper()
        except Exception:
            team_short = str(team_id)

    # prediction
    expected_points = None
    xpts_breakdown = None
    prediction_source = None
    data_quality = None
    minutes_estimate = None
    start_prob = None
    mat = _load_prediction_materialized(db, int(player_id), int(gameweek))
    if mat is not None:
        if mat.get("expected_points") is not None:
            expected_points = round(float(mat["expected_points"]), 2)
        prediction_source = mat.get("source")
        data_quality = mat.get("data_quality")
        if mat.get("minutes_estimate") is not None:
            minutes_estimate = round(float(mat["minutes_estimate"]), 1)
        if mat.get("start_prob") is not None:
            start_prob = round(float(mat["start_prob"]), 3)
        raw_bd = mat.get("breakdown")
        if isinstance(raw_bd, dict) and raw_bd:
            xpts_breakdown = {k: round(float(v), 2) for k, v in raw_bd.items()}
    # fallback to inline provider when materialized row missing (needed for tests with StaticProvider)
    if expected_points is None:
        try:
            prov = deps.get_prediction_provider(db)
            preds = prov.get_squad_predictions([int(player_id)], [int(gameweek)])
            p = (preds.get(int(gameweek)) or {}).get(int(player_id))
            if p is not None:
                expected_points = round(float(p.expected_points), 2)
                prediction_source = getattr(p, "source", prediction_source) or prediction_source
                data_quality = getattr(p, "data_quality", data_quality) or data_quality
                if getattr(p, "expected_minutes", None) is not None:
                    minutes_estimate = round(float(p.expected_minutes), 1)
                if getattr(p, "start_probability", None) is not None:
                    start_prob = round(float(p.start_probability), 3)
                bd = getattr(p, "breakdown", None)
                if isinstance(bd, dict) and bd:
                    xpts_breakdown = {k: round(float(v), 2) for k, v in bd.items()}
        except Exception:
            pass

    # Understat xG/xA
    xg = xa = None
    try:
        provider = deps.get_prediction_provider(db)
        idx_getter = getattr(provider, "understat_index", None)
        if callable(idx_getter):
            uindex = idx_getter() or {}
            from fpl_intelligence.data_providers.understat import UnderstatConnector, build_stats_from_row
            urow = UnderstatConnector.match_player(uindex, web_name)
            if urow is not None:
                stats = build_stats_from_row(urow)
                xg = round(float(stats.xg_per_90), 2)
                xa = round(float(stats.xa_per_90), 2)
    except Exception:
        pass

    # form bars
    form_bars = _form_bars_from_history(db, int(player_id))

    # fixtures
    fixture_runs: list[dict[str, Any]] = []
    avg_fdr = NEUTRAL_FDR
    try:
        # need squad gameweek for horizon; use passed gameweek
        team_names = _team_names(db)
        # load fixtures synchronously? load_fixtures is async
        import asyncio
        raw_fixtures = None
        try:
            # if we are already in async context, we need to run via run_until_complete
            # but this endpoint is async, so we can await directly - we will handle outside
            pass
        except Exception:
            pass
    except Exception:
        pass
    # fixtures will be filled by caller that awaits load_fixtures
    # For now return empty and let caller populate? Instead we make payload builder async and caller will fill fixtures.
    # To avoid complexity, we keep fixture_runs empty here and enhance in route.

    # set pieces
    set_pieces = _set_piece_for(int(player_id), team_id)

    # news flag (materialized)
    news_flag = None
    try:
        from fpl_intelligence.api.routes.news import cached_items_from_db
        from fpl_intelligence.data_providers.bbc_news import NEWS_KEYWORDS, match_headlines
        from fpl_intelligence.materialize.service import NEWS_MAX_AGE_SECONDS
        items, fetched_at = cached_items_from_db(db, max_age_seconds=NEWS_MAX_AGE_SECONDS)
        if items and web_name:
            flags = match_headlines(items, [(player_id, web_name, first_name, second_name)], NEWS_KEYWORDS)
            hit = flags.get(str(player_id))
            if hit is not None:
                news_flag = hit
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        news_flag = None

    return {
        "id": int(player_id),
        "web_name": web_name,
        "team": team_id,
        "team_short": team_short,
        "position": position,
        "price": price,
        "expected_points": expected_points,
        "prediction_source": prediction_source,
        "data_quality": data_quality,
        "minutes_estimate": minutes_estimate,
        "start_prob": start_prob,
        "xpts_breakdown": xpts_breakdown,
        "xg_per_90": xg,
        "xa_per_90": xa,
        "selected_by_percent": selected_by,
        "form_bars": form_bars,
        "fixture_runs": fixture_runs,
        "avg_fdr": avg_fdr,
        "news_flag": news_flag,
        "set_pieces": set_pieces,
    }

@router.get("", include_in_schema=False)
async def compare_players(
    db: GetDB,
    player_a: int = Query(..., description="First player element id"),
    player_b: int = Query(..., description="Second player element id"),
    session_id: str | None = Query(None),
    gw: int | None = Query(None, description="Gameweek override"),
):
    # resolve gameweek
    target_gw = None
    if gw is not None:
        target_gw = int(gw)
    elif session_id:
        squad = SquadService(session=db).get_squad(session_id=session_id)
        if squad is not None:
            target_gw = int(squad.gameweek)
            # try to resolve to current GW via clock
            try:
                from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek
                target_gw = await resolve_target_gameweek(db, fallback=target_gw)
            except Exception:
                pass
    if target_gw is None:
        try:
            from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek
            target_gw = await resolve_target_gameweek(db, fallback=2)
        except Exception:
            target_gw = 2

    # build base payloads
    payload_a = _player_payload(db, int(player_a), int(target_gw))
    payload_b = _player_payload(db, int(player_b), int(target_gw))

    # fixtures: async load
    try:
        raw_fixtures = await load_fixtures(db)
        rows = parse_fixtures(raw_fixtures)
        team_names = _team_names(db)
        if rows:
            # determine current horizon
            sq_gw = target_gw
            current = max(
                min((r.event for r in rows if not r.finished), default=sq_gw),
                sq_gw,
            )
            horizon = next_gameweeks(rows, current, HORIZON_GWS)
            rows_by_gw: dict[int, list[Any]] = {}
            for r in rows:
                rows_by_gw.setdefault(r.event, []).append(r)
            for payload in (payload_a, payload_b):
                team = payload.get("team")
                runs = [r for r in player_run(team, rows_by_gw, horizon, team_names=team_names)]
                payload["fixture_runs"] = [r.__dict__ for r in runs]
                real = [r for r in runs if r.opponent_id != 0]
                if real:
                    payload["avg_fdr"] = round(average_fdr(real), 2)
                else:
                    payload["avg_fdr"] = NEUTRAL_FDR
                # if empty, fabricate neutral
                if not payload["fixture_runs"]:
                    payload["fixture_runs"] = [
                        {"gw": target_gw + i, "opponent_id": 0, "opponent": "—", "is_home": True, "difficulty": 3}
                        for i in range(HORIZON_GWS)
                    ]
    except Exception as exc:
        logger.warning("compare fixtures failed: %s", exc)
        for payload in (payload_a, payload_b):
            if not payload.get("fixture_runs"):
                payload["fixture_runs"] = [
                    {"gw": target_gw + i, "opponent_id": 0, "opponent": "—", "is_home": True, "difficulty": 3}
                    for i in range(HORIZON_GWS)
                ]

    # diff metadata
    def _diff(field: str):
        av = payload_a.get(field)
        bv = payload_b.get(field)
        # handle string percentages
        if field == "selected_by_percent":
            try:
                av = float(str(av).strip("%")) if av is not None else None
            except Exception:
                av = None
            try:
                bv = float(str(bv).strip("%")) if bv is not None else None
            except Exception:
                bv = None
        if av is None or bv is None:
            return None
        try:
            avf = float(av)
            bvf = float(bv)
        except Exception:
            return None
        if avf > bvf:
            return "a"
        if bvf > avf:
            return "b"
        return "tie"

    diff = {
        "expected_points": _diff("expected_points"),
        "price": _diff("price"),
        "xg_per_90": _diff("xg_per_90"),
        "xa_per_90": _diff("xa_per_90"),
        "selected_by_percent": _diff("selected_by_percent"),
    }

    return {
        "gameweek": target_gw,
        "player_a": payload_a,
        "player_b": payload_b,
        "diff": diff,
    }
