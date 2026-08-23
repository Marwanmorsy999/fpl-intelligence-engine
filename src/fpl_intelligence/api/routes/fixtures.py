"""Phase 20.0 — fixture scanner endpoint.

``GET /api/v1/fixtures/scan?session_id=`` returns, for the saved squad:

* per-player next-5 fixture runs (opponent, home/away, FDR 1-5),
* a squad swing score (positive = easy patch),
* the top-5 easiest team runs over the next 4 gameweeks (transfer targets).

Data is the free official FPL ``/api/fixtures/`` payload fetched through the
egress chain; the raw payload is cached in-process so repeated scans are cheap.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.config import get_settings
from fpl_intelligence.fixtures.scanner import (
    NEUTRAL_FDR,
    average_fdr,
    easiest_team_runs,
    infer_current_gameweek,
    next_gameweeks,
    parse_fixtures,
    player_run,
    squad_swing_score,
)
from fpl_intelligence.squad.service import SquadService

router = APIRouter(prefix="/fixtures", tags=["fixtures"])
logger = logging.getLogger(__name__)

GetDB = deps.GetDB

#: Horizon sizes from the phase spec.
PLAYER_HORIZON_GWS = 5
TEAM_HORIZON_GWS = 4
TOP_TEAM_RUNS = 5

#: In-process cache of the raw FPL fixtures payload (shared across requests).
_fixtures_lock = threading.Lock()
_fixtures_cache: tuple[float, list[dict[str, Any]]] | None = None


def _cached_fixtures(max_age_seconds: float) -> list[dict[str, Any]]:
    """Return cached raw fixtures when fresh; ``[]`` otherwise."""
    global _fixtures_cache
    with _fixtures_lock:
        if _fixtures_cache is not None and time.monotonic() - _fixtures_cache[0] < max_age_seconds:
            return _fixtures_cache[1]
    return []


def _store_fixtures(raw: Any) -> None:
    global _fixtures_cache
    if isinstance(raw, list):
        with _fixtures_lock:
            _fixtures_cache = (time.monotonic(), raw)


async def load_fixtures() -> list[dict[str, Any]]:
    """Raw FPL fixtures payload through the egress chain, cache-first."""
    settings = get_settings()
    cached = _cached_fixtures(settings.egress_cache_ttl or 300)
    if cached:
        return cached
    from fpl_intelligence.data_providers.fpl_egress import FplEgressChain  # noqa: PLC0415

    egress = FplEgressChain(
        settings.fpl_base_url,
        timeout=settings.egress_strategy_timeout,
        cache_ttl=settings.egress_cache_ttl,
    )
    try:
        raw = await egress.fetch("/api/fixtures/")
    except Exception as exc:  # noqa: BLE001 - surfaced as an honest 503
        logger.warning("fixtures fetch failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Fixture data unavailable right now (FPL blocked or offline).",
        ) from exc
    _store_fixtures(raw)
    return raw if isinstance(raw, list) else []


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

    rows = parse_fixtures(await load_fixtures())
    if not rows:
        raise HTTPException(status_code=503, detail="No upcoming fixtures published yet.")
    current_gw = max(infer_current_gameweek(rows), squad.gameweek)
    horizon = next_gameweeks(rows, current_gw, PLAYER_HORIZON_GWS)
    team_horizon = next_gameeeks_safe(rows, current_gw)
    rows_by_gw: dict[int, list[Any]] = {}
    for row in rows:
        rows_by_gw.setdefault(row.event, []).append(row)

    prices = squad.player_prices or {}
    positions = squad.player_positions or {}

    players_out: list[dict[str, Any]] = []
    starter_avgs: list[float] = []
    for idx, pid in enumerate(squad.player_ids):
        team = (squad.player_teams or {}).get(pid)
        runs = player_run(team, rows_by_gw, horizon)
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
    targets = easiest_team_runs(rows_by_gw, team_horizon, top=TOP_TEAM_RUNS, exclude_teams=exclude)

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
    """Team-horizon helper kept tiny for readability."""
    return next_gameweeks(rows, current_gw, TEAM_HORIZON_GWS)


def _resolve_player_names(db: Session, pids: list[int]) -> dict[int, str]:
    from sqlalchemy import select  # noqa: PLC0415

    from fpl_intelligence.db.models import Player  # noqa: PLC0415

    names: dict[int, str] = {}
    for pid in set(pids):
        row = db.scalar(select(Player).where(Player.fpl_element_id == pid)) or db.get(Player, pid)
        if row is not None:
            names[pid] = row.web_name
    return names
