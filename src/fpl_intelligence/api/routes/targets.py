"""Phase 25 Gate 0 (T2) — /api/v1/targets — the ALPHA ENGINE endpoint.

Serves the /targets page: top-10 ranked by Alpha with every input term,
recent volatility, next-3 FDR strip, one-line reason, affordability vs the
user's bank, position-need weighting, and a Next-GW focus trio computed for
the next unplayed gameweek from the gameweek clock (never hardcoded).

Honesty contract: ownership/volatility/FDR with no data are ``None`` and the
UI renders an "unavailable" chip; nothing is fabricated.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from fpl_intelligence.alpha import service as alpha_service
from fpl_intelligence.api import deps
from fpl_intelligence.fixtures.scanner import (
    NEUTRAL_FDR_INT,
    parse_fixtures,
    player_run,
)
from fpl_intelligence.prediction.live_provider import load_player_catalog
from fpl_intelligence.squad.service import SquadService
from fpl_intelligence.sync.materialized_models import PredictionCurrentDB
from fpl_intelligence.sync.models import IngestedGameweekDB

router = APIRouter(prefix="/targets", tags=["targets"])
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
        logger.warning("predictions_current read failed: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return {}


def _rival_picks(db: Any, session_id: str) -> dict[str, list[int]]:
    """Cached picks of the user's default/remembered league (may be empty)."""
    try:
        from fpl_intelligence.leagues.models import LeagueCacheDB, LeagueSelectionDB
        from fpl_intelligence.leagues.service import (
            pick_default_league,
            stored_entry_leagues,
        )

        leagues = stored_entry_leagues(db, session_id)
        if not leagues:
            return {}
        sel = db.get(LeagueSelectionDB, str(session_id))
        chosen = None
        if sel is not None:
            chosen = next((lg for lg in leagues if lg["league_id"] == sel.league_id), None)
        target = chosen or pick_default_league(leagues)
        if not target:
            return {}
        row = db.get(LeagueCacheDB, int(target["league_id"]))
        picks = (row.rivals_picks or {}).get("picks") if row else None
        if not isinstance(picks, dict):
            return {}
        return {
            k: [int(p) for p in v]
            for k, v in picks.items()
            if isinstance(v, list)
        }
    except Exception as exc:  # noqa: BLE001 — league layer optional
        logger.warning("rival picks unavailable: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return {}


def _history_points(db: Any, element_ids: set[int]) -> dict[int, list[int]]:
    """Last-5 actual points per element from ingested_history (desc→asc)."""
    out: dict[int, list[int]] = {}
    if not element_ids:
        return out
    try:
        rows = db.execute(
            select(
                IngestedGameweekDB.element_id,
                IngestedGameweekDB.gameweek,
                IngestedGameweekDB.total_points,
            )
            .where(IngestedGameweekDB.element_id.in_(element_ids))  # type: ignore[attr-defined]
            .order_by(IngestedGameweekDB.gameweek.desc())
            .limit(len(element_ids) * 5 * 3)
        ).all()
        for eid, _gw, pts in rows:
            bucket = out.setdefault(int(eid), [])
            if len(bucket) < 5 and pts is not None:
                bucket.append(int(pts))
    except Exception as exc:  # noqa: BLE001 — cold table must not 500
        logger.warning("ingested_history read failed: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
    return out


def _fdr_runs(
    db: Any, team_of: dict[int, int], horizon_gws: list[int]
) -> dict[int, list[dict[str, Any]]]:
    """Next-horizon fixture runs per player (max 3 GWs)."""
    runs_by_player: dict[int, list[dict[str, Any]]] = {}
    horizon = horizon_gws[:3]
    if not horizon:
        return runs_by_player
    raw: Any = []
    try:
        from fpl_intelligence.sync.materialized_models import FixturesCacheDB

        row = db.scalar(select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()).limit(1))
        raw = row.payload if row is not None else []
    except Exception as exc:  # noqa: BLE001 — fixtures optional
        logger.warning("fixtures cache read failed: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return runs_by_player
    try:
        rows = parse_fixtures(raw or [])
        by_gw: dict[int, list[Any]] = {}
        for r in rows:
            by_gw.setdefault(r.event, []).append(r)
        from fpl_intelligence.api.routes.fixtures import _team_names

        team_names = _team_names(db)
        teams_needed = {tid for tid in team_of.values() if tid}
        for pid, tid in team_of.items():
            if tid and tid in teams_needed:
                runs = [r.__dict__ for r in player_run(tid, by_gw, horizon, team_names=team_names)]
            else:
                runs = [
                    {"gw": gw, "opponent": "—", "is_home": True, "difficulty": NEUTRAL_FDR_INT}
                    for gw in horizon
                ]
            runs_by_player[pid] = runs
    except Exception as exc:  # noqa: BLE001 — fixtures optional
        logger.warning("fixture run projection failed: %s", exc)
    return runs_by_player


@router.get("", include_in_schema=False)
async def targets_overview(
    response: Response,
    db: deps.GetDB,
    session_id: str | None = Query(None, description="Session key (saved squad)."),
    show_all: bool = Query(False, description="Include unaffordable targets."),
    limit: int = Query(10, ge=1, le=600),
) -> dict[str, Any]:
    """Top transfer targets ranked by Alpha with full provenance."""
    response.headers["Cache-Control"] = "no-store"

    # v2.7.3-dual-state: user-facing Alpha reads the local override (effective squad)
    squad = SquadService(session=db).get_effective_squad(session_id=session_id) if session_id else None
    bank = float(squad.bank or 0.0) if squad else 0.0
    squad_ids = list(squad.player_ids or []) if squad else []
    pos_counts: dict[int, int] = {}
    if squad:
        for pid in squad_ids:
            pos = (squad.player_positions or {}).get(pid)
            if pos:
                pos_counts[int(pos)] = pos_counts.get(int(pos), 0) + 1

    from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

    fallback_gw = int(squad.gameweek) if squad else 1
    target_gw = await resolve_target_gameweek(db, fallback=fallback_gw)

    catalog = load_player_catalog()
    xpts = _xpts_map(db, target_gw)
    pos_of = {pid: int(row["position"]) for pid, row in catalog.items() if row.get("position")}
    # Players only known to the DB keep their squad-declared position.
    if squad and squad.player_positions:
        for pid, pos in squad.player_positions.items():
            pos_of.setdefault(int(pid), int(pos))

    rival_picks = _rival_picks(db, str(session_id)) if session_id else {}
    history = _history_points(db, set(xpts.keys()))
    need_weights = alpha_service.position_need_boost(pos_counts)

    pos_avg = alpha_service.position_average(xpts, pos_of)
    sell_value: dict[int, float] = {}
    if squad:
        prices = squad.player_prices or {}
        for pid, price in prices.items():
            pos = pos_of.get(int(pid))
            if pos is None:
                continue
            cur = sell_value.get(int(pos))
            val = float(price)
            if cur is None or val < cur:
                sell_value[int(pos)] = val

    candidates: list[dict[str, Any]] = []
    for pid, xp in xpts.items():
        cat = catalog.get(pid, {})
        pos = pos_of.get(pid)
        if pos is None or pos not in pos_avg:
            continue
        own, own_label = alpha_service.league_ownership(
            pid, rival_picks, cat.get("selected_by_percent")
        )
        alpha, terms = alpha_service.alpha_score(xp, pos_avg[pos], own)
        vol = alpha_service.recent_volatility(history.get(pid, []))
        price = float(cat.get("price") or 0.0)
        max_afford = bank + (sell_value.get(pos, 0.0) if sell_value else 0.0)
        affordability = "bank"
        if price > bank + 1e-9:
            affordability = "needs-sale" if price <= max_afford + 1e-9 else "unaffordable"
        weight = need_weights.get(pos, 1.0)
        candidates.append(
            {
                "player_id": pid,
                "web_name": cat.get("web_name") or f"Player {pid}",
                "position": POS_NAMES.get(pos, str(pos)),
                "position_code": pos,
                "team": cat.get("team_short") or "",
                "price": round(price, 1),
                "xpts": terms["xpts"],
                "pos_avg": terms["pos_avg"],
                "edge": terms["edge"],
                "own_p": terms["own_p"],
                "ownership_label": own_label,
                "alpha": alpha,
                "rank_score": (alpha if alpha is not None else terms["edge"]) * weight,
                "volatility": vol,
                "affordability": affordability,
                "need_weight": round(weight, 3),
                "user_owns": pid in set(squad_ids),
            }
        )

    candidates.sort(key=lambda c: -c["rank_score"])
    visible = [
        c
        for c in candidates
        if show_all or c["affordability"] != "unaffordable" or c["user_owns"]
    ]

    team_of = {
        c["player_id"]: (catalog.get(c["player_id"], {}) or {}).get("team")
        for c in visible[:40]
    }
    runs = _fdr_runs(db, {k: v for k, v in team_of.items() if v}, _next_unplayed_gws(db, target_gw))

    top: list[dict[str, Any]] = []
    for c in visible[: int(limit)]:
        c = dict(c)
        c.pop("rank_score", None)
        c["fixture_strip"] = runs.get(c["player_id"], [])
        c["reason"] = _reason_line(c)
        c["how_computed"] = (
            "Alpha = (xPTS − position avg xPTS) × (1 − ownership); "
            "volatility = std-dev of last-5 actual points; "
            "ranking boosts your thinnest rostered position (+20% max)"
        )
        top.append(c)

    next_gw_focus = _next_gw_focus(db, target_gw, catalog, rival_picks, squad_ids)

    return {
        "session_id": session_id,
        "gameweek": target_gw,
        "bank": round(bank, 1),
        "show_all": bool(show_all),
        "league_rivals_cached": len(rival_picks),
        "min_league_rivals": alpha_service.MIN_LEAGUE_RIVALS,
        "position_avgs": {POS_NAMES.get(p, p): v for p, v in sorted(pos_avg.items())},
        "need_weights": {POS_NAMES.get(p, p): w for p, w in sorted(need_weights.items())},
        "targets": top,
        "hidden_count": len(visible) - len(top),
        "next_gw_focus": next_gw_focus,
        "generated_note": "Alpha engine — every term shown; unavailable inputs render as chips",
    }


def _next_unplayed_gws(db: Any, current: int) -> list[int]:
    try:
        from fpl_intelligence.fixtures.scanner import next_unplayed_gameweeks, parse_fixtures
        from fpl_intelligence.sync.materialized_models import FixturesCacheDB

        row = db.scalar(select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()).limit(1))
        if row is None or not row.payload:
            return [current]
        return next_unplayed_gameweeks(parse_fixtures(row.payload), current, 3) or [current]
    except Exception:  # noqa: BLE001 — honest neutral fallback
        return [current]


def _reason_line(c: dict[str, Any]) -> str:
    parts: list[str] = []
    edge = c.get("edge")
    if edge is not None:
        verb = "above" if edge >= 0 else "below"
        parts.append(f"{abs(edge):.1f} xPTS {verb} {c['position']} average")
    own = c.get("own_p")
    if own is not None:
        pct = f"{own * 100:.0f}%"
        league_owned = c.get("ownership_label") == "league ownership"
        label = "league-owned" if league_owned else "globally owned"
        parts.append(f"{pct} {label}")
    if c.get("volatility") is not None:
        parts.append(f"volatility {c['volatility']:.1f}")
    return " · ".join(parts) or "insufficient data for a reasoned line"


def _next_gw_focus(
    db: Any,
    target_gw: int,
    catalog: dict[int, dict[str, Any]],
    rival_picks: dict[str, list[int]],
    squad_ids: list[int],
) -> dict[str, Any]:
    """Top-3 buys specifically for the next unplayed GW (clock-driven)."""
    focus_gws = _next_unplayed_gws(db, target_gw)
    focus_gw = focus_gws[0] if focus_gws else target_gw
    xpts = _xpts_map(db, focus_gw)
    pos_of = {pid: int(row["position"]) for pid, row in catalog.items() if row.get("position")}
    pos_avg = alpha_service.position_average(xpts, pos_of)
    picks: list[dict[str, Any]] = []
    for pid, xp in xpts.items():
        pos = pos_of.get(pid)
        if pos is None or pos not in pos_avg:
            continue
        own, own_label = alpha_service.league_ownership(
            pid, rival_picks, catalog.get(pid, {}).get("selected_by_percent")
        )
        alpha, terms = alpha_service.alpha_score(xp, pos_avg[pos], own)
        picks.append(
            {
                "player_id": pid,
                "web_name": catalog.get(pid, {}).get("web_name") or f"Player {pid}",
                "price": catalog.get(pid, {}).get("price"),
                "gameweek": focus_gw,
                "alpha": alpha,
                "edge": terms["edge"],
                "own_p": terms["own_p"],
                "user_owns": pid in set(squad_ids),
            }
        )
    picks.sort(key=lambda p: -(p["alpha"] if p["alpha"] is not None else p["edge"]))
    return {
        "gameweek": focus_gw,
        "buys": picks[:3],
        "how_computed": "top-3 Alpha buys for the first unplayed gameweek per the official clock",
    }


@router.get("/squad-metrics", include_in_schema=False)
async def squad_metrics(
    response: Response,
    db: deps.GetDB,
    session_id: str = Query(..., description="Session key (saved squad)."),
) -> dict[str, Any]:
    """Per-squad-player {xPTS, Alpha, price, volatility} for the sort view.

    v2.7.3-dual-state: reads the effective (local-preferred) squad.
    """
    response.headers["Cache-Control"] = "no-store"
    squad = SquadService(session=db).get_effective_squad(session_id=session_id)
    if squad is None:
        return {"session_id": session_id, "status": "no-squad", "metrics": {}}

    from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

    target_gw = await resolve_target_gameweek(db, fallback=int(squad.gameweek))
    catalog = load_player_catalog()
    xpts = _xpts_map(db, target_gw)
    pos_of = {pid: int(row["position"]) for pid, row in catalog.items() if row.get("position")}
    if squad.player_positions:
        for pid, pos in squad.player_positions.items():
            pos_of.setdefault(int(pid), int(pos))
    pos_avg = alpha_service.position_average(xpts, pos_of)
    rival_picks = _rival_picks(db, str(session_id))
    history = _history_points(db, set(squad.player_ids or []))
    prices = squad.player_prices or {}

    metrics: dict[str, Any] = {}
    for pid in squad.player_ids or []:
        xp = xpts.get(int(pid))
        pos = pos_of.get(int(pid))
        alpha = None
        edge = None
        if xp is not None and pos in pos_avg:
            alpha, terms = alpha_service.alpha_score(xp, pos_avg[pos], None)
            # Ownership-aware alpha when we can compute it.
            own, _label = alpha_service.league_ownership(
                int(pid), rival_picks, catalog.get(int(pid), {}).get("selected_by_percent")
            )
            if own is not None:
                alpha = round(terms["edge"] * (1.0 - own), 3)
            edge = terms["edge"]
        vol = alpha_service.recent_volatility(history.get(int(pid), []))
        metrics[str(pid)] = {
            "xpts": round(xp, 2) if xp is not None else None,
            "alpha": alpha,
            "edge": edge,
            "price": prices.get(int(pid)),
            "volatility": vol,
        }
    return {
        "session_id": session_id,
        "status": "ok",
        "gameweek": target_gw,
        "metrics": metrics,
        "how_computed": (
            "xPTS from materialized predictions; Alpha = (xPTS − pos avg) × "
            "(1 − ownership) where ownership exists; volatility = std-dev of "
            "last-5 actual points"
        ),
    }
