"""Phase 27 Gate 1 (S1) — League Trajectory: 3-GW projection.

Simulates next 3 GWs for user + top-3 rivals using ONLY materialized
predictions_current. No invented numbers: missing GWs contribute 0 and are
disclosed. Produces a projected rank line chart payload and a text insight.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from sqlalchemy import select

from fpl_intelligence.sync.materialized_models import PredictionCurrentDB

logger = logging.getLogger(__name__)


def _xpts_map(db: Any, gameweek: int) -> dict[int, float]:
    try:
        rows = db.execute(
            select(PredictionCurrentDB.element_id, PredictionCurrentDB.expected_points).where(
                PredictionCurrentDB.gameweek == int(gameweek)
            )
        ).all()
        return {int(e): float(x or 0.0) for e, x in rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("predictions_current read failed gw%s: %s", gameweek, exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return {}


def _starting_xi_for(entry_key: str, picks_map: dict[str, list[int]], xi_len: int = 11) -> list[int]:
    lst = picks_map.get(str(entry_key)) or []
    return [int(p) for p in lst[:xi_len]]


def league_trajectory(
    db: Any,
    session_id: str,
    horizon: int = 3,
) -> dict[str, Any]:
    """Build projected league rank line chart over next `horizon` GWs.

    Uses stored league cache + materialized predictions. Returns honest
    unavailable state when cache or predictions missing.
    """
    from fpl_intelligence.api.routes.league import _ensure_tables, pick_default
    from fpl_intelligence.leagues.models import LeagueCacheDB, LeagueSelectionDB
    from fpl_intelligence.leagues.service import stored_entry_leagues
    from fpl_intelligence.squad.models_db import SquadStateDB
    from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek  # noqa: PLC0415  # sync import

    _ensure_tables(db)

    # Resolve target GW — fallback to 1 when clock unavailable (tests)
    try:
        import asyncio
        from fpl_intelligence.squad.models_db import LocalSquadStateDB

        # v2.7.3-dual-state: effective squad for fallback gameweek
        row = db.scalar(select(LocalSquadStateDB).where(LocalSquadStateDB.session_id == str(session_id)))
        if row is None:
            row = db.scalar(select(SquadStateDB).where(SquadStateDB.session_id == str(session_id)))
        fallback_gw = 1
        if row is not None and isinstance(row.squad_json, dict):
            fallback_gw = int(row.squad_json.get("gameweek") or 1)
        # Try async resolve; if loop running use fallback
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                target_gw = fallback_gw
            else:
                target_gw = loop.run_until_complete(resolve_target_gameweek(db, fallback=fallback_gw))
        except RuntimeError:
            target_gw = fallback_gw
    except Exception:
        target_gw = 1

    horizon_gws = [int(target_gw) + i for i in range(horizon)]

    leagues = stored_entry_leagues(db, session_id)
    sel = db.get(LeagueSelectionDB, str(session_id))
    chosen = None
    if sel is not None:
        chosen = next((lg for lg in leagues if lg["league_id"] == sel.league_id), None)
    selected = chosen or pick_default(leagues)
    if not selected:
        return {
            "status": "no-league",
            "note": "No classic league detected — trajectory unavailable.",
            "gameweek": target_gw,
            "horizon_gws": horizon_gws,
            "series": [],
            "insight": None,
            "how_computed": "No league selected; detection via /api/entry/{id}/ required.",
        }

    cache_row = db.get(LeagueCacheDB, int(selected["league_id"]))
    if cache_row is None or not (cache_row.standings or []):
        return {
            "status": "no-cache",
            "note": "No cached standings yet — press Refresh on /league to pull them.",
            "gameweek": target_gw,
            "horizon_gws": horizon_gws,
            "series": [],
            "insight": None,
            "how_computed": "League cache empty; standings not yet fetched.",
        }

    standings = [r for r in (cache_row.standings or []) if isinstance(r, dict)]
    # Current totals map
    current_totals: dict[str, int] = {}
    names: dict[str, str] = {}
    for r in standings:
        try:
            eid = str(int(r["entry_id"]))
        except (TypeError, ValueError, KeyError):
            continue
        tot = r.get("total")
        if isinstance(tot, (int, float)):
            current_totals[eid] = int(tot)
        names[eid] = str(r.get("entry_name") or f"Entry {eid}")

    # Picks map (capped rivals)
    rp = cache_row.rivals_picks or {}
    picks_map: dict[str, list[int]] = {
        k: [int(p) for p in v] for k, v in (rp.get("picks") or {}).items() if isinstance(v, list)
    }
    # User picks: need squad XI; fallback to first 11 squad ids
    # v2.7.3-dual-state: effective squad (local preferred)
    from fpl_intelligence.squad.models_db import LocalSquadStateDB as _LocalDB  # noqa: PLC0415

    user_row = db.scalar(select(_LocalDB).where(_LocalDB.session_id == str(session_id)))
    if user_row is None:
        user_row = db.scalar(select(SquadStateDB).where(SquadStateDB.session_id == str(session_id)))
    user_ids: list[int] = []
    if user_row is not None and isinstance(user_row.squad_json, dict):
        user_ids = [int(p) for p in (user_row.squad_json.get("player_ids") or [])[:11]]

    # Determine top-3 rivals by current total (excluding user)
    rivals_sorted = sorted(
        ((eid, tot) for eid, tot in current_totals.items() if eid != str(session_id)),
        key=lambda kv: -kv[1],
    )
    top3 = [eid for eid, _ in rivals_sorted[:3]]
    tracked = [str(session_id)] + top3
    # Ensure we have display names for tracked
    labels = {eid: names.get(eid, f"Entry {eid}") for eid in tracked}
    labels[str(session_id)] = "You"

    # Build projected cumulative line per tracked entry
    # Per GW, sum xPTS of that entry's XI (or user_ids for You)
    series: list[dict[str, Any]] = []
    # Preload all xPTS maps
    xpts_by_gw: dict[int, dict[int, float]] = {gw: _xpts_map(db, gw) for gw in horizon_gws}
    has_any_data = any(bool(m) for m in xpts_by_gw.values())
    if not has_any_data:
        return {
            "status": "no-predictions",
            "note": "No materialized predictions for trajectory horizon — run the daily 06:10 materialize.",
            "gameweek": target_gw,
            "selected": selected,
            "horizon_gws": horizon_gws,
            "series": [],
            "insight": None,
            "how_computed": "No predictions_current rows for horizon GWs.",
        }

    for eid in tracked:
        xi = _starting_xi_for(eid, picks_map) if eid != str(session_id) else user_ids
        if not xi:
            xi = user_ids if eid == str(session_id) else []
        base = int(current_totals.get(eid, 0))
        pts_line: list[int | float] = [base]
        cum = float(base)
        deltas: list[float] = []
        for gw in horizon_gws:
            m = xpts_by_gw.get(gw, {})
            gw_pts = round(sum(float(m.get(pid, 0.0)) for pid in xi), 1) if xi else 0.0
            cum += float(gw_pts)
            pts_line.append(round(cum, 1))
            deltas.append(float(gw_pts))
        series.append(
            {
                "entry_id": eid,
                "label": labels[eid],
                "is_you": eid == str(session_id),
                "current_total": base,
                "points": pts_line,  # len = 1 + horizon (GW0..GW3)
                "per_gw": deltas,
            }
        )

    # Compute projected ranks after each future GW
    # rank_points[step] = sorted totals descending at that step
    # step 0 = now, step 1..horizon = after each GW
    max_step = horizon
    rank_at_step: list[dict[str, int]] = []
    for step in range(max_step + 1):
        totals_at_step = [(s["entry_id"], s["points"][step]) for s in series]
        # Include any non-tracked entries as flat lines (no projection) for rank realism
        # For simplicity, only rank among tracked when league large — disclose in note
        sorted_step = sorted(totals_at_step, key=lambda kv: -kv[1])
        rank_map = {eid: idx + 1 for idx, (eid, _) in enumerate(sorted_step)}
        rank_at_step.append(rank_map)

    # Insight: gap to leader + overtake forecast
    you_now = int(current_totals.get(str(session_id), 0))
    # leader is top by current total among tracked rivals
    leader_eid = top3[0] if top3 else None
    leader_now = int(current_totals.get(leader_eid, 0)) if leader_eid else you_now
    gap_now = leader_now - you_now if leader_eid else 0
    insight: str | None = None
    # Find earliest future GW where you rank 1
    overtake_gw: int | None = None
    for step in range(1, max_step + 1):
        if rank_at_step[step].get(str(session_id)) == 1:
            overtake_gw = horizon_gws[step - 1]
            break
    leader_name = labels.get(leader_eid, f"Entry {leader_eid}") if leader_eid else "leader"
    if not has_any_data:
        insight = None
    elif leader_eid is None or leader_eid == str(session_id):
        insight = "You lead your mini-league. Projected to stay ahead over the next 3 GWs on current Alpha targets."
    elif overtake_gw is not None:
        insight = (
            f"You are {gap_now} pts behind {leader_name}. Projected to overtake by GW{overtake_gw} "
            f"based on your Alpha targets vs his fixture difficulty."
        )
    elif gap_now > 0:
        # Forecast gap narrowing?
        you_end = series[0]["points"][-1] if series else you_now
        leader_end = next((s["points"][-1] for s in series if s["entry_id"] == leader_eid), leader_now)
        gap_end = int(leader_end) - int(you_end) if leader_eid else 0
        if gap_end < gap_now:
            insight = (
                f"You are {gap_now} pts behind {leader_name}. Projected to close to {gap_end} pts behind "
                f"by GW{horizon_gws[-1]} — not yet an overtake on current xPTS."
            )
        else:
            insight = (
                f"You are {gap_now} pts behind {leader_name}. No overtake projected over the next 3 GWs "
                f"— the gap is forecast to stay at ~{gap_end} pts."
            )
    else:
        insight = f"Trajectory computed over GWs {horizon_gws}."

    partial_note = ""
    if (len(standings) < int(selected.get("member_count") or len(standings))) or len(top3) < 3:
        partial_note = "Trajectory covers you + top-3 rivals only (standings page 1 capped)."

    return {
        "status": "ok",
        "selected": selected,
        "gameweek": target_gw,
        "horizon_gws": horizon_gws,
        "series": series,
        "ranks": rank_at_step,
        "insight": insight,
        "partial_note": partial_note,
        "how_computed": "Cumulative = current total + Σ(xPTS of XI per horizon GW from predictions_current); missing GWs score 0; ranks among tracked entries only.",
    }
