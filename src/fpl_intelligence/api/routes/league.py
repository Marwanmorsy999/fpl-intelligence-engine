"""Phase 23 Gate 1 (L1) — LEAGUE KILLER endpoints.

Zero config: classic leagues are AUTO-DETECTED from
``/api/entry/{entry}/leagues/`` through the egress masks (daily cron + first
visit), never hardcoded. When an entry belongs to several leagues the page
shows a picker whose choice is remembered; the default skips global system
leagues (``Overall``, ``Gameweek 1`` …) and picks the user's private classic
league (biggest private; else biggest non-global).

Honest states throughout: private/blocked leagues, cache age chips,
"refreshing…" during on-demand refreshes (10-minute cooldown) and a note when
the league is larger than the cached standings page.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from fpl_intelligence.api import deps
from fpl_intelligence.leagues.models import LeagueCacheDB, LeagueSelectionDB
from fpl_intelligence.leagues.service import (
    REFRESH_COOLDOWN_SECONDS,
    RIVALS_CAP,
    fetch_entry_leagues,
    ownership_insights,
    projected_edge_lines,
    refresh_league_cache,
    stored_entry_leagues,
    upsert_entry_leagues,
)

router = APIRouter(prefix="/league", tags=["league"])
logger = logging.getLogger(__name__)

#: In-process cooldown guard so concurrent visits share one refresh.
_refresh_marks: dict[int, float] = {}
_refresh_lock = threading.Lock()

#: Inline refresh budget (serverless-safe; the response waits for it).
_REFRESH_INLINE_BUDGET = 38.0


def _ensure_tables(db: Any) -> None:
    """Self-sealing DDL for deployments whose DB predates Phase 23."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    ddl = (
        """
        CREATE TABLE IF NOT EXISTS entry_leagues (
            id SERIAL PRIMARY KEY,
            entry_id INTEGER NOT NULL,
            league_id INTEGER NOT NULL,
            league_name VARCHAR(255) NOT NULL DEFAULT '',
            member_count INTEGER,
            entry_rank INTEGER,
            entry_last_rank INTEGER,
            private BOOLEAN NOT NULL DEFAULT FALSE,
            discovered_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT uq_entry_league UNIQUE (entry_id, league_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS league_cache (
            league_id INTEGER PRIMARY KEY,
            name VARCHAR(255) NOT NULL DEFAULT '',
            member_count INTEGER,
            standings JSONB NOT NULL DEFAULT '[]'::jsonb,
            rivals_picks JSONB NOT NULL DEFAULT '{}'::jsonb,
            refreshed_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS league_selection (
            session_id VARCHAR(255) PRIMARY KEY,
            league_id INTEGER NOT NULL,
            chosen_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
        """,
    )
    insp = sa_inspect(db.get_bind())
    try:
        for statement in ddl:
            table = statement.split("EXISTS")[1].split("(")[0].strip()
            if insp.has_table(table):
                continue
            db.execute(text(statement))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — sqlite tests pre-create tables
        db.rollback()
        logger.debug("league DDL skipped: %s", exc)


async def _target_gameweek(db: Any, fallback: int) -> int:
    from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

    return int(await resolve_target_gameweek(db, fallback=int(fallback)))


def _user_squad(db: Any, session_key: str) -> dict[str, Any] | None:
    """v2.7.3-dual-state: effective squad (local preferred, base fallback).

    v2.7.4-prod-heal: fully self-sealing and never-raising. Prod DBs that
    predate migration 0021 lack ``local_squad_state``; the read degrades to
    the base row and finally to ``None`` instead of surfacing a 500.
    """
    from fpl_intelligence.squad.models_db import LocalSquadStateDB, SquadStateDB
    from fpl_intelligence.squad.service import SquadService

    try:
        local_row = db.scalar(
            select(LocalSquadStateDB).where(LocalSquadStateDB.session_id == str(session_key))
        )
        if local_row is not None and isinstance(local_row.squad_json, dict):
            return local_row.squad_json
    except Exception as exc:  # noqa: BLE001 — table may not exist yet on prod
        logger.warning("local_squad_state read failed; re-sealing table: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
        SquadService._ensure_local_table(db)
        try:
            local_row = db.scalar(
                select(LocalSquadStateDB).where(
                    LocalSquadStateDB.session_id == str(session_key)
                )
            )
            if local_row is not None and isinstance(local_row.squad_json, dict):
                return local_row.squad_json
        except Exception as exc2:  # noqa: BLE001 — degrade to base squad
            logger.warning("local_squad_state still unreadable, using base: %s", exc2)
            with contextlib.suppress(Exception):
                db.rollback()
    try:
        row = db.scalar(
            select(SquadStateDB).where(SquadStateDB.session_id == str(session_key))
        )
    except Exception as exc:  # noqa: BLE001 — never fail a league render
        logger.warning("squad_state read failed; treating as no squad: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return None
    if row is None or not isinstance(row.squad_json, dict):
        return None
    return row.squad_json


def _stored_xi(db: Any, session_key: str, gameweek: int) -> tuple[list[int], str]:
    """Stored engine-recommended XI, falling back to the fielded starters."""
    from fpl_intelligence.sync.models import RecommendationDB

    try:
        row = db.scalar(
            select(RecommendationDB).where(
                RecommendationDB.session_key == str(session_key),
                RecommendationDB.gameweek == int(gameweek),
                RecommendationDB.rec_type == "xi",
            )
        )
    except Exception as exc:  # noqa: BLE001 — v2.7.4-prod-heal never-500
        logger.warning("recommendation xi read failed: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
        row = None
    if row is not None and isinstance(row.subject, dict):
        xi = [int(p) for p in (row.subject.get("xi") or [])]
        if xi:
            return xi, "recommended-xi"
    squad = _user_squad(db, session_key)
    if squad and int(squad.get("gameweek") or 0) == int(gameweek):
        ids = [int(p) for p in (squad.get("player_ids") or [])][:11]
        if ids:
            return ids, "fielded-starters"
    return [], "unknown"


def _xpts_map(db: Any, gameweek: int) -> dict[int, float]:
    from fpl_intelligence.sync.materialized_models import PredictionCurrentDB

    try:
        rows = db.execute(
            select(
                PredictionCurrentDB.element_id,
                PredictionCurrentDB.expected_points,
            ).where(PredictionCurrentDB.gameweek == int(gameweek))
        ).all()
    except Exception as exc:  # noqa: BLE001 — v2.7.4-prod-heal never-500
        logger.warning("predictions_current read failed gw%s: %s", gameweek, exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return {}
    return {int(e): float(x or 0.0) for e, x in rows}


def _name_map(db: Any) -> dict[int, str]:
    from fpl_intelligence.prediction.live_provider import load_player_catalog

    names: dict[int, str] = {}
    try:
        for pid, row in load_player_catalog().items():
            name = str(row.get("web_name") or "")
            if name:
                names[int(pid)] = name
    except Exception:  # noqa: BLE001 — display-only enrichment
        pass
    return names


def _stored_captain(db: Any, session_key: str, gameweek: int) -> int | None:
    from fpl_intelligence.sync.models import RecommendationDB

    try:
        row = db.scalar(
            select(RecommendationDB.subject).where(
                RecommendationDB.session_key == str(session_key),
                RecommendationDB.gameweek == int(gameweek),
                RecommendationDB.rec_type == "captain",
            )
        )
    except Exception as exc:  # noqa: BLE001 — v2.7.4-prod-heal never-500
        logger.warning("captain recommendation read failed: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return None
    if isinstance(row, dict):
        cap = int(row.get("captain_id") or 0)
        return cap or None
    return None


class LeagueSelectBody(BaseModel):
    session_id: str = Field(..., min_length=1)
    league_id: int = Field(..., gt=0)


@router.get("")
@router.get("/")
async def league_overview(
    response: Response,
    db: deps.GetDB,
    session_id: str = Query(..., description="FPL entry id (= saved session key)."),
    league_id: int | None = Query(None, description="Override the remembered choice."),
    refresh: bool = Query(False, description="Trigger an on-demand refresh."),
) -> dict[str, Any]:
    """Everything the /league page renders, served from the Postgres cache.

    v2.7.4-prod-heal NEVER-500 contract: any unexpected failure returns a
    200 payload with an honest degraded chip instead of an error page.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return await _league_overview_impl(db, session_id, league_id, refresh)
    except Exception as exc:  # noqa: BLE001 — degrade honestly, never 500
        with contextlib.suppress(Exception):
            db.rollback()
        logger.exception("GET /league failed; serving degraded payload")
        return {
            "session_id": session_id,
            "status": "degraded",
            "leagues": [],
            "selected": None,
            "needs_picker": False,
            "rivals_cap": RIVALS_CAP,
            "cache_age_seconds": None,
            "note": (
                f"league data stale since {datetime.now(UTC).isoformat(timespec='seconds')} "
                f"— render failed ({type(exc).__name__}); retry or press Refresh"
            ),
            "diag": f"{type(exc).__name__}: {exc}",
            "honest_notes": [
                "League page could not be computed right now — showing a "
                "degraded state rather than failing."
            ],
        }


async def _league_overview_impl(
    db: deps.GetDB,
    session_id: str,
    league_id: int | None,
    refresh: bool,
) -> dict[str, Any]:
    _ensure_tables(db)

    # --- 0. auto-detected leagues (cached rows; live discovery on miss) ------
    leagues = stored_entry_leagues(db, session_id)
    detection_note = ""
    if not leagues:
        try:
            discovered = await asyncio.wait_for(
                fetch_entry_leagues(int(session_id)), timeout=8.0
            )
            if discovered:
                upsert_entry_leagues(db, int(session_id), discovered)
                leagues = stored_entry_leagues(db, session_id)
                detection_note = f"auto-detected {len(leagues)} classic league(s)"
        except Exception as exc:  # noqa: BLE001 — honest blocked state
            detection_note = f"league auto-detect failed ({type(exc).__name__})"

    selection_row = db.get(LeagueSelectionDB, str(session_id))
    chosen: dict[str, Any] | None = None
    if league_id is not None:
        chosen = next(
            (lg for lg in leagues if lg["league_id"] == int(league_id)), None
        )
    elif selection_row is not None:
        chosen = next(
            (lg for lg in leagues if lg["league_id"] == selection_row.league_id),
            None,
        )
    default = pick_default(leagues)
    selected = chosen or default

    payload: dict[str, Any] = {
        "session_id": session_id,
        "leagues": leagues,
        "selected": selected,
        "needs_picker": bool(len(leagues) > 1 and chosen is None and league_id is None),
        "default_rule": "private classic league, globals skipped",
        "detection_note": detection_note,
        "rivals_cap": RIVALS_CAP,
        "cache_age_seconds": None,
        "status": "no-league",
    }
    if not selected:
        payload["note"] = detection_note or (
            "No classic league detected yet — the daily job discovers them "
            "from your FPL entry automatically."
        )
        return payload

    cache_row = db.get(LeagueCacheDB, int(selected["league_id"]))
    fresh = False
    should_refresh = cache_row is None or not (cache_row.standings or [])
    if cache_row is not None and (cache_row.standings or []):
        fetched = cache_row.refreshed_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        age = max(0.0, (datetime.now(UTC) - fetched).total_seconds())
        payload["cache_age_seconds"] = round(age, 1)
        fresh = age < REFRESH_COOLDOWN_SECONDS
        should_refresh = not fresh

    if should_refresh and refresh:
        lid = int(selected["league_id"])
        # Wall-clock cooldown guard: monotonic() has no defined epoch and on
        # freshly-started serverless workers can sit below the threshold,
        # which permanently read "cooldown active" on a clean instance.
        now_wall = time.time()
        with _refresh_lock:
            last = _refresh_marks.get(lid, 0.0)
            cooling = now_wall - last < REFRESH_COOLDOWN_SECONDS
            if not cooling:
                _refresh_marks[lid] = now_wall
        diag = ""
        if not cooling:
            target_gw = await _target_gameweek(db, 1)
            try:
                await asyncio.wait_for(
                    refresh_league_cache(db, lid, target_gw),
                    timeout=_REFRESH_INLINE_BUDGET,
                )
                cache_row = db.get(LeagueCacheDB, lid)
                if cache_row is not None and (cache_row.standings or []):
                    fetched = cache_row.refreshed_at
                    if fetched.tzinfo is None:
                        fetched = fetched.replace(tzinfo=UTC)
                    age = max(
                        0.0, (datetime.now(UTC) - fetched).total_seconds()
                    )
                    payload["cache_age_seconds"] = round(age, 1)
                    payload["status"] = "ok"
                    picks_gw = int(
                        (cache_row.rivals_picks or {}).get("gameweek")
                        or target_gw
                    )
                    payload.update(
                        _build_view(
                            db,
                            cache_row,
                            session_id,
                            gameweek=picks_gw,
                        )
                    )
                    return payload
                diag = "refresh finished without standings"
            except TimeoutError:
                diag = "inline budget exceeded"
            except Exception as exc:  # noqa: BLE001 — surfaced honestly below
                db.rollback()
                diag = f"{type(exc).__name__}: {exc}"
        payload["status"] = "refreshing"
        payload["note"] = (
            "refreshing… (the standings pull is still warming — reload in a "
            "few seconds)"
        )
        payload["diag"] = diag or "cooldown active — retry shortly"
        return payload

    if cache_row is None or not (cache_row.standings or []):
        payload["status"] = "stale"
        payload["note"] = "No cached standings yet — press Refresh to pull them now."
        return payload

    payload["status"] = "ok" if fresh else "stale"
    payload.update(
        _build_view(
            db,
            cache_row,
            session_id,
            gameweek=int((cache_row.rivals_picks or {}).get("gameweek")
                         or await _target_gameweek(db, 1)),
        )
    )
    return payload


def pick_default(leagues: list[dict[str, Any]]) -> dict[str, Any] | None:
    from fpl_intelligence.leagues.service import pick_default_league

    return pick_default_league(leagues)


def _log_refresh_failure(task: asyncio.Task[Any]) -> None:
    exc = task.exception() if not task.cancelled() else None
    if exc is not None:
        logger.warning("on-demand league refresh failed: %s", exc)


def _build_view(
    db: Any,
    cache_row: LeagueCacheDB,
    session_key: str,
    *,
    gameweek: int,
) -> dict[str, Any]:
    standings = [r for r in (cache_row.standings or []) if isinstance(r, dict)]
    rp = cache_row.rivals_picks or {}
    picks_map: dict[str, list[int]] = {
        k: [int(p) for p in v]
        for k, v in (rp.get("picks") or {}).items()
        if isinstance(v, list)
    }
    captains: dict[str, int] = {
        k: int(v) for k, v in (rp.get("captains") or {}).items() if v
    }

    mine = next((r for r in standings if int(r["entry_id"]) == int(session_key)), None)
    user_rank = int(mine["rank"]) if mine and mine.get("rank") is not None else None
    gap_to_third = None
    if len(standings) >= 3 and mine is not None:
        third_total = standings[2].get("total")
        my_total = mine.get("total")
        if third_total is not None and my_total is not None:
            gap_to_third = int(third_total) - int(my_total)

    squad = _user_squad(db, session_key)
    user_ids = {int(p) for p in ((squad or {}).get("player_ids") or [])}
    names = _name_map(db)
    insights = ownership_insights(user_ids, picks_map, names, top_n=3)

    rec_xi, rec_source = _stored_xi(db, session_key, gameweek)
    xpts = _xpts_map(db, gameweek)
    rival_names = {
        k: next(
            (r["entry_name"] for r in standings if int(r["entry_id"]) == int(k)),
            f"Entry {k}",
        )
        for k in picks_map
    }
    edge = projected_edge_lines(rec_xi, xpts, picks_map, rival_names)

    all_rival_players: set[int] = set()
    for ids in picks_map.values():
        all_rival_players.update(ids)
    my_differentials = [
        {"web_name": names.get(pid, f"Player {pid}"), "player_id": pid}
        for pid in sorted(user_ids - all_rival_players)[:5]
    ]

    my_captain = _stored_captain(db, session_key, gameweek)

    note_parts: list[str] = []
    if rp.get("partial"):
        note_parts.append(
            "league larger than the cached page — showing standings page 1 only"
        )

    return {
        "league_name": cache_row.name,
        "member_count": cache_row.member_count,
        "standings_top": standings[:10],
        "your_rank": user_rank,
        "gap_to_top3": gap_to_third,
        "ownership_insights": insights,
        "your_differentials": my_differentials,
        "projected_edge": edge,
        "captain_insight": _captain_insight(
            my_captain, captains, names, len(picks_map)
        ),
        "recommended_xi_source": rec_source,
        "picks_gameweek": rp.get("gameweek"),
        "honest_notes": note_parts,
    }


def _captain_insight(
    my_captain: int | None,
    rival_captains: dict[str, int],
    names: dict[int, str],
    rivals_count: int,
) -> dict[str, Any] | None:
    if not my_captain or not rivals_count:
        return None
    same = sum(1 for c in rival_captains.values() if int(c) == int(my_captain))
    label = names.get(int(my_captain), f"Player {my_captain}")
    if same == 0:
        line = (
            f"{label} is your captain differential — none of "
            f"{rivals_count} top rivals captain him"
        )
    else:
        line = f"{same} of {rivals_count} top rivals also captain {label}"
    return {
        "captain_id": int(my_captain),
        "captain_name": label,
        "matching_rivals": same,
        "rivals": rivals_count,
        "line": line,
    }


@router.post("/select")
async def league_select(body: LeagueSelectBody, db: deps.GetDB) -> dict[str, Any]:
    """Remember the picker choice per session."""
    _ensure_tables(db)
    db.merge(
        LeagueSelectionDB(
            session_id=str(body.session_id),
            league_id=int(body.league_id),
            chosen_at=datetime.now(UTC),
        )
    )
    db.commit()
    return {"ok": True, "session_id": body.session_id, "league_id": body.league_id}


class LeagueRefreshBody(BaseModel):
    session_id: str = Field(..., description="FPL entry id (= session key)")
    league_id: int | None = Field(None, description="Override the remembered choice")
    gameweek: int | None = Field(None, description="Target GW for picks (defaults to clock)")


@router.post("/refresh", include_in_schema=False)
async def league_refresh(
    body: LeagueRefreshBody, db: deps.GetDB, response: Response
) -> dict[str, Any]:
    """v2.7.3-dual-state: Refresh league standing + rival picks (no squad write).

    This is what the renamed "Refresh League Data" button calls. It fetches
    standings page 1 and capped rival picks via the egress masks, persists them
    to ``league_cache`` and returns the same payload shape as ``GET /league``.
    The user's ``local_squad`` is never touched.
    """
    response.headers["Cache-Control"] = "no-store"
    _ensure_tables(db)
    # Ensure we have discovered leagues for this entry.
    from fpl_intelligence.leagues.service import stored_entry_leagues as _stored  # noqa: PLC0415

    leagues = _stored(db, body.session_id)
    if not leagues:
        try:
            discovered = await asyncio.wait_for(
                fetch_entry_leagues(int(body.session_id)), timeout=8.0
            )
            if discovered:
                upsert_entry_leagues(db, int(body.session_id), discovered)
                leagues = _stored(db, body.session_id)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "note": f"league auto-detect failed ({type(exc).__name__})", "session_id": body.session_id}

    target_league_id = body.league_id
    if target_league_id is None:
        sel = db.get(LeagueSelectionDB, str(body.session_id))
        if sel is not None:
            target_league_id = sel.league_id
        else:
            default = pick_default(leagues)
            target_league_id = int(default["league_id"]) if default else None
    if target_league_id is None:
        return {"status": "no-league", "note": "No classic league detected yet.", "session_id": body.session_id}

    target_gw = body.gameweek
    if target_gw is None:
        target_gw = await _target_gameweek(db, 1)

    try:
        await asyncio.wait_for(
            refresh_league_cache(db, int(target_league_id), int(target_gw)),
            timeout=_REFRESH_INLINE_BUDGET,
        )
    except TimeoutError:
        return {"status": "refreshing", "note": "Inline budget exceeded — retry shortly.", "league_id": target_league_id}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return {"status": "error", "note": f"{type(exc).__name__}: {exc}", "league_id": target_league_id}

    # Return fresh view (mirrors GET /league success path)
    cache_row = db.get(LeagueCacheDB, int(target_league_id))
    if cache_row is None or not (cache_row.standings or []):
        return {"status": "stale", "note": "No cached standings yet.", "league_id": target_league_id}
    view = _build_view(
        db,
        cache_row,
        str(body.session_id),
        gameweek=int((cache_row.rivals_picks or {}).get("gameweek") or target_gw),
    )
    return {"status": "ok", "league_id": target_league_id, "gameweek": target_gw, **view}


@router.get("/trajectory", include_in_schema=False)
async def league_trajectory_route(
    response: Response,
    db: deps.GetDB,
    session_id: str = Query(..., description="FPL entry id (= saved session key)."),
) -> dict[str, Any]:
    """Phase 27 Gate 1 (S1) — projected league rank over next 3 GWs.

    v2.7.3-dual-state: simulates the user's *effective* (local-preferred) XI
    against auto-fetched rival XIs.
    v2.7.4-prod-heal NEVER-500 contract: any failure returns a 200 payload
    with an honest "trajectory unavailable" chip.
    """
    response.headers["Cache-Control"] = "no-store"
    from fpl_intelligence.leagues.trajectory import league_trajectory

    try:
        return league_trajectory(db, session_id)
    except Exception as exc:  # noqa: BLE001 — degrade honestly, never 500
        with contextlib.suppress(Exception):
            db.rollback()
        logger.exception("GET /league/trajectory failed; serving degraded payload")
        return {
            "session_id": session_id,
            "status": "unavailable",
            "note": f"trajectory unavailable: {type(exc).__name__}: {exc}",
            "series": [],
            "insight": None,
            "horizon_gws": [],
            "how_computed": (
                "Projection needs league cache + predictions_current + squad "
                "rows; one of them failed to read."
            ),
        }


@router.get("/fomo", include_in_schema=False)
async def league_fomo(
    response: Response,
    db: deps.GetDB,
    session_id: str = Query(..., description="FPL entry id (= saved session key)."),
    gameweek: int | None = Query(None, description="Gameweek to grade (defaults to latest ingested)."),
) -> dict[str, Any]:
    """Phase 27 Gate 1 (S2) — FOMO & Regret engine."""
    response.headers["Cache-Control"] = "no-store"
    from fpl_intelligence.track_record.fomo import compute_regret

    return compute_regret(db, session_id, gameweek)
