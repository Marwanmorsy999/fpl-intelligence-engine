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
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import delete
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.config import get_settings
from fpl_intelligence.fixtures.scanner import (
    NEUTRAL_FDR,
    TEAM_SHORT_NAMES,
    average_fdr,
    easiest_team_runs,
    infer_current_gameweek,
    next_gameweeks,
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
    fetches back into the cache for the next caller.
    """
    cached = load_cached_fixtures(db)
    if cached:
        return cached

    settings = get_settings()
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
    current_gw = max(infer_current_gameweek(rows), squad.gameweek)
    horizon = next_gameweeks(rows, current_gw, PLAYER_HORIZON_GWS)
    team_horizon = next_gameeeks_safe(rows, current_gw)
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
