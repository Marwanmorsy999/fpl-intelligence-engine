"""Phase 20.0 — fixture scanner endpoint.

``GET /api/v1/fixtures/scan?session_id=`` returns, for the saved squad:

* per-player next-5 fixture runs (opponent, home/away, FDR 1-5),
* a squad swing score (positive = easy patch),
* the top-5 easiest team runs over the next 4 gameweeks (transfer targets).

Phase 20.1: data comes from the materialized ``fixtures_cache`` table (written
by the daily 06:10 cron from vaastav raw.githubusercontent) — zero live
network fetches in the request path. Team short names come from the DB Team
table (official bootstrap ids), never from a hardcoded season-specific map.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import delete
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.fixtures.scanner import (
    NEUTRAL_FDR,
    TEAM_SHORT_NAMES,
    average_fdr,
    easiest_team_runs,
    infer_current_gameweek,
    next_unplayed_gameweeks,
    parse_fixtures,
    player_run,
    squad_swing_score,
)
from fpl_intelligence.materialize import load_cached_fixtures, team_names_from_db
from fpl_intelligence.squad.service import SquadService
from fpl_intelligence.sync.materialized_models import FixturesCacheDB

router = APIRouter(prefix="/fixtures", tags=["fixtures"])
logger = logging.getLogger(__name__)

GetDB = deps.GetDB

#: Horizon sizes from the phase spec.
PLAYER_HORIZON_GWS = 5
TEAM_HORIZON_GWS = 4
TOP_TEAM_RUNS = 5


def _team_names(db: Session) -> dict[int, str]:
    """DB-backed official-id -> short-name map; falls back to the static map.

    The fallback only fills gaps so an unseeded deployment still renders
    something instead of ``T7``-style placeholders for every team.
    """
    names = team_names_from_db(db)
    if not names:
        return dict(TEAM_SHORT_NAMES)
    merged = dict(TEAM_SHORT_NAMES)
    merged.update(names)
    return merged


async def load_fixtures(db: Session) -> list[dict[str, Any]]:
    """Raw FPL fixtures payload, materialized-cache-first (Phase 20.1).

    Reads ``fixtures_cache`` (written by the 06:10 cron from vaastav) so the
    request path performs ZERO live network fetches. Falls back to the egress
    chain only when no cached payload exists yet — and stores whatever it
    fetches back into the cache for the next caller. Under pytest the egress
    fallback is disabled so the suite stays hermetic.
    """
    cached = load_cached_fixtures(db)
    if cached:
        return cached

    import sys

    if "pytest" in sys.modules or os.getenv("FPL_NO_NETWORK", "") == "1":
        return []

    from fpl_intelligence.data_providers.registry import get_async_fpl_adapter

    try:
        raw = await get_async_fpl_adapter().fetch("/api/fixtures/", capability="fixtures")
    except Exception as exc:  # noqa: BLE001 - surfaced as an honest 503
        logger.warning("fixtures fetch failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Fixture data unavailable right now (FPL blocked or offline).",
        ) from exc

    # Backfill the materialized cache so subsequent requests stay off-network.
    if isinstance(raw, list) and raw:
        try:
            db.execute(delete(FixturesCacheDB))
            db.add(
                FixturesCacheDB(
                    source="egress-backfill",
                    payload=raw,
                    fetched_at=datetime.now(UTC),
                )
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 — caching is best-effort
            logger.warning("fixtures cache backfill failed: %s", exc)
            db.rollback()
    return raw if isinstance(raw, list) else []


# --------------------------------------------------------------------------- #
# Phase 4 — per-player / per-team fixture feed for My Team & cards
# --------------------------------------------------------------------------- #

_FIXTURES_PER_PLAYER = 3


def _resolve_player_teams(
    db: Session, pids: list[int], hint_teams: dict[int, int] | None = None
) -> dict[int, int]:
    """Resolve each player id to its FPL team id (ElementFactDB → catalog)."""
    teams: dict[int, int] = {}
    # Prefer caller-supplied hints (squad.player_teams) — cheapest + authoritative.
    if hint_teams:
        for pid in pids:
            t = hint_teams.get(pid)
            if t:
                teams[pid] = int(t)
    missing = [pid for pid in pids if pid not in teams]
    if missing:
        try:
            from fpl_intelligence.sync.materialized_models import ElementFactDB

            for element_id, team_id in db.execute(
                select(ElementFactDB.element_id, ElementFactDB.team_id).where(
                    ElementFactDB.element_id.in_(missing)
                )
            ).all():
                if element_id is not None and team_id is not None:
                    teams[int(element_id)] = int(team_id)
        except Exception as exc:  # noqa: BLE001 — metadata only
            db.rollback()
            logger.debug("element_facts team read failed: %s", exc)
    still_missing = [pid for pid in missing if pid not in teams]
    if still_missing:
        try:
            from fpl_intelligence.prediction.live_provider import load_player_catalog

            for pid in still_missing:
                row = load_player_catalog().get(int(pid))
                if row and row.get("team") is not None:
                    teams[int(pid)] = int(row["team"])
        except Exception as exc:  # noqa: BLE001 — metadata only
            logger.debug("seed catalog team read failed: %s", exc)
    return teams


def _fixtures_for_players(
    db: Session,
    rows: list[Any],
    team_names: dict[int, str],
    players: dict[int, int],
    horizon: list[int],
    rows_by_gw: dict[int, list[Any]],
) -> dict[str, Any]:
    """Build {player_id: {team_id, fixtures:[...up to N...]}} keyed by str(id)."""
    out: dict[str, Any] = {}
    for pid, team_id in players.items():
        if team_id:
            runs = player_run(team_id, rows_by_gw, horizon, team_names=team_names)
        else:
            runs = []
        real_runs = [r for r in runs if r.opponent_id != 0][: _FIXTURES_PER_PLAYER]
        out[str(pid)] = {
            "team_id": team_id or None,
            "fixtures": [
                {
                    "gw": r.gw,
                    "opponent": r.opponent,
                    "opponent_id": r.opponent_id,
                    "is_home": r.is_home,
                    "difficulty": r.difficulty,
                    "kickoff": None,
                }
                for r in real_runs
            ],
        }
    return out


@router.get("", summary="Per-player / per-team upcoming fixtures")
@router.get("/", summary="Per-player / per-team upcoming fixtures")
async def fixtures_get(
    db: GetDB,
    response: Response,
    session_id: str | None = Query(None, description="Per-user session key (resolves the effective FPL 15)."),
    player_ids: str | None = Query(None, description="Comma-separated player ids, e.g. '1,2,3'."),
    team_id: int | None = Query(None, description="Return upcoming fixtures for this FPL team id."),
) -> dict[str, Any]:
    """Next 1–3 fixtures per player (or per team).

    Phase 4 — restores the fixtures feed that the My Team page renders. Accepts
    EITHER a ``session_id`` (resolves the effective FPL 15), OR an explicit
    ``player_ids`` list, OR a single ``team_id``. When no fixtures are
    published yet the response is still a 200 with empty lists so the UI can
    render "TBD" honestly instead of a 404.
    """
    if not session_id and not player_ids and team_id is None:
        raise HTTPException(
            status_code=400,
            detail="Provide one of: session_id, player_ids, or team_id.",
        )

    squad_players: dict[int, int] = {}  # pid -> team_id
    if session_id:
        squad = SquadService(session=db).get_effective_squad(session_id=session_id, mode="fpl")
        if squad is None:
            raise HTTPException(
                status_code=404, detail="No squad saved for this session"
            )
        squad_players = _resolve_player_teams(
            db, list(squad.player_ids), hint_teams=squad.player_teams
        )
    elif player_ids:
        try:
            pids = [int(x) for x in str(player_ids).split(",") if x.strip()]
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="player_ids must be comma-separated integers."
            ) from exc
        squad_players = _resolve_player_teams(db, pids)

    rows = parse_fixtures(await load_fixtures(db))
    team_names = _team_names(db)
    rows_by_gw = {}
    for row in rows:
        rows_by_gw.setdefault(row.event, []).append(row)
    try:
        from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

        target_gw = await resolve_target_gameweek(db, fallback=1)
    except Exception:
        target_gw = 1
    current_gw = max(infer_current_gameweek(rows), target_gw)
    horizon = next_unplayed_gameweeks(rows, current_gw, _FIXTURES_PER_PLAYER)

    response.headers["Cache-Control"] = "no-store"
    by_player = _fixtures_for_players(
        db, rows, team_names, squad_players, horizon, rows_by_gw
    )
    by_team: dict[str, list[dict[str, Any]]] = {}
    if team_id is not None:
        runs = player_run(int(team_id), rows_by_gw, horizon, team_names=team_names)
        by_team[str(team_id)] = [
            {
                "gw": r.gw,
                "opponent": r.opponent,
                "opponent_id": r.opponent_id,
                "is_home": r.is_home,
                "difficulty": r.difficulty,
                "kickoff": None,
            }
            for r in runs
            if r.opponent_id != 0
        ][: _FIXTURES_PER_PLAYER]
    return {
        "session_id": session_id,
        "gameweek": current_gw,
        "horizon_gws": horizon,
        "by_player": by_player,
        "by_team": by_team,
    }


@router.get("/scan")
async def fixture_scan(
    db: GetDB,
    response: Response,
    session_id: str | None = Query(None, description="Per-user session key. Required."),
) -> dict[str, Any]:
    """Scan the saved squad's next-5 fixture difficulty plus league-wide runs."""
    if not session_id:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    response.headers["Cache-Control"] = "no-store"

    rows = parse_fixtures(await load_fixtures(db))
    if not rows:
        raise HTTPException(status_code=503, detail="No upcoming fixtures published yet.")
    # Phase 21.1 (T2): the target GW follows the official FPL clock at request
    # time; the horizon shows the next five gameweeks with UNPLAYED fixtures.
    try:
        from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

        target_gw = await resolve_target_gameweek(db, fallback=int(squad.gameweek))
    except Exception:
        target_gw = int(squad.gameweek)
    current_gw = max(infer_current_gameweek(rows), target_gw, int(squad.gameweek))
    horizon = next_unplayed_gameweeks(rows, current_gw, PLAYER_HORIZON_GWS)
    team_horizon = next_unplayed_gameweeks(rows, current_gw, TEAM_HORIZON_GWS)
    team_names = _team_names(db)
    rows_by_gw: dict[int, list[Any]] = {}
    for row in rows:
        rows_by_gw.setdefault(row.event, []).append(row)

    prices = squad.player_prices or {}
    positions = squad.player_positions or {}

    players_out: list[dict[str, Any]] = []
    starter_avgs: list[float] = []
    for idx, pid in enumerate(squad.player_ids):
        team = (squad.player_teams or {}).get(pid)
        runs = player_run(team, rows_by_gw, horizon, team_names=team_names)
        real_runs = [r for r in runs if r.opponent_id != 0]
        avg = round(average_fdr(real_runs), 2) if real_runs else NEUTRAL_FDR
        # First 11 entries are starters under the FPL picks convention.
        if idx < 11:
            starter_avgs.append(avg)
        players_out.append(
            {
                "player_id": pid,
                "web_name": "",
                "position": positions.get(pid),
                "price": prices.get(pid),
                "is_starter": idx < 11,
                "runs": [r.__dict__ for r in runs],
                "avg_fdr": avg,
                "swing": round(NEUTRAL_FDR - avg, 2),
            }
        )

    # Fill display names from the ingested player table (best effort).
    names = _resolve_player_names(db, squad.player_ids)
    for p in players_out:
        p["web_name"] = names.get(p["player_id"], f"Player {p['player_id']}")

    exclude = {t for t in (squad.player_teams or {}).values() if t}
    targets = easiest_team_runs(
        rows_by_gw,
        team_horizon,
        top=TOP_TEAM_RUNS,
        exclude_teams=exclude,
        team_names=team_names,
    )

    return {
        "session_id": session_id,
        "gameweek": current_gw,
        "horizon_gws": horizon,
        "players": players_out,
        "squad_swing_score": squad_swing_score(starter_avgs),
        "easiest_runs": [
            {
                "team_id": t.team_id,
                "short_name": t.short_name,
                "avg_fdr": t.avg_fdr,
                "runs": [r.__dict__ for r in t.runs],
            }
            for t in targets
        ],
        "scanned_at": datetime.now(UTC).isoformat(),
    }


def next_gameeeks_safe(rows: Any, current_gw: int) -> list[int]:
    """Team-horizon helper kept tiny for readability (unplayed-only)."""
    return next_unplayed_gameweeks(rows, current_gw, TEAM_HORIZON_GWS)


def _resolve_player_names(db: Session, pids: list[int]) -> dict[int, str]:
    """Resolve player names in bulk while preserving legacy-id fallback semantics."""
    from sqlalchemy import select  # noqa: PLC0415

    from fpl_intelligence.db.models import Player  # noqa: PLC0415

    unique_pids = sorted(set(int(pid) for pid in pids))
    if not unique_pids:
        return {}

    names: dict[int, str] = {}

    rows = db.scalars(
        select(Player).where(Player.fpl_element_id.in_(unique_pids))
    ).all()
    for row in rows:
        if row.fpl_element_id is not None:
            names[int(row.fpl_element_id)] = row.web_name

    missing = [pid for pid in unique_pids if pid not in names]
    if missing:
        legacy_rows = db.scalars(select(Player).where(Player.id.in_(missing))).all()
        for row in legacy_rows:
            if row.id is not None and row.web_name:
                names[int(row.id)] = row.web_name

    return names
