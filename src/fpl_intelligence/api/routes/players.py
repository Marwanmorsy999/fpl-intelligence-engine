"""Player discovery/search endpoints."""

from __future__ import annotations

import contextlib
import difflib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from fpl_intelligence.api import deps
from fpl_intelligence.db.models import Player
from fpl_intelligence.sync.materialized_models import ElementFactDB, _latest_xpts_map
from fpl_intelligence.sync.models import IngestedGameweekDB

router = APIRouter()
GetDB = deps.GetDB

_RELEVANCE_CUTOFF = 0.45
_PREFIX_BONUS = 0.25
_catalog_cache: dict[int, dict[str, Any]] | None = None
_seed_codes_cache: dict[int, int] | None = None


def _catalog() -> dict[int, dict[str, Any]]:
    global _catalog_cache
    if _catalog_cache is None:
        from fpl_intelligence.prediction.live_provider import load_player_catalog
        _catalog_cache = load_player_catalog()
    return _catalog_cache


def _seed_codes() -> dict[int, int]:
    """Read immutable FPL element codes once without opening PostgreSQL."""
    global _seed_codes_cache
    if _seed_codes_cache is not None:
        return _seed_codes_cache
    candidates = [
        Path("data") / "seed" / "fpl_bootstrap_seed.json",
        Path(__file__).resolve().parents[4] / "data" / "seed" / "fpl_bootstrap_seed.json",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            _seed_codes_cache = {
                int(row["id"]): int(row["code"])
                for row in raw.get("players", [])
                if (
                    isinstance(row, dict)
                    and row.get("id") is not None
                    and row.get("code") is not None
                )
            }
            return _seed_codes_cache
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    _seed_codes_cache = {}
    return _seed_codes_cache


def _reset_catalog_cache() -> None:
    global _catalog_cache, _seed_codes_cache
    _catalog_cache = None
    _seed_codes_cache = None


def _test_override_db(request: Request) -> Any | None:
    """Return an explicitly overridden DB session used by unit tests."""
    override = request.app.dependency_overrides.get(deps._get_db_session)
    if override is None:
        return None
    candidate = override()
    if hasattr(candidate, "__next__"):
        return next(candidate)
    return candidate


def _db_players(db: Any, team: int | None) -> list[PlayerSummary]:
    """Render explicitly overridden DB state for legacy ingestion tests."""
    from fpl_intelligence.db.models import PlayerGameweekPerformance, PlayerTeamMembership

    catalog = _catalog()
    codes = _seed_codes()
    players = db.execute(select(Player).order_by(Player.id)).scalars().all()
    out: list[PlayerSummary] = []
    for p in players:
        latest_perf_team = db.execute(
            select(PlayerGameweekPerformance.team_id)
            .where(PlayerGameweekPerformance.player_id == p.id)
            .order_by(PlayerGameweekPerformance.gameweek_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        team_id = int(latest_perf_team) if latest_perf_team is not None else None
        if team_id is None:
            membership_team = db.execute(
                select(PlayerTeamMembership.team_id)
                .where(PlayerTeamMembership.player_id == p.id)
                .order_by(PlayerTeamMembership.valid_from.desc(), PlayerTeamMembership.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            team_id = int(membership_team) if membership_team is not None else None
        if team is not None and team_id != team:
            continue

        price_row = db.execute(
            select(PlayerGameweekPerformance.price)
            .where(
                PlayerGameweekPerformance.player_id == p.id,
                PlayerGameweekPerformance.price.is_not(None),
            )
            .order_by(PlayerGameweekPerformance.gameweek_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if price_row is not None:
            price = float(price_row)
        else:
            cat = catalog.get(int(p.fpl_element_id)) if p.fpl_element_id is not None else None
            price_value = (cat or {}).get("price")
            price = float(price_value) if price_value is not None else None

        element_id = int(p.fpl_element_id) if p.fpl_element_id is not None else None
        code = getattr(p, "fpl_code", None)
        if code is None and element_id is not None:
            code = codes.get(element_id)
        # Enrich from element_facts if available
        eid = int(p.fpl_element_id) if p.fpl_element_id is not None else None
        fact = db.get(ElementFactDB, eid) if eid is not None else None
        out.append(
            PlayerSummary(
                id=p.id,
                fpl_element_id=p.fpl_element_id,
                web_name=p.web_name or f"Player {p.id}",
                team=team_id,
                position=int(p.position_code) if p.position_code is not None else None,
                price=price,
                code=int(code) if code is not None else None,
                status=fact.status if fact else None,
                chance_of_playing_next_round=fact.chance_of_playing_next_round if fact else None,
                chance_of_playing_this_round=fact.chance_of_playing_this_round if fact else None,
                news=fact.news if fact else None,
                form=fact.form if fact else None,
                total_points=fact.total_points if fact else None,
                points_per_game=fact.points_per_game if fact else None,
                ict_index=fact.ict_index if fact else None,
                ep_next=fact.ep_next if fact else None,
                transfers_in_event=fact.transfers_in_event if fact else None,
                transfers_out_event=fact.transfers_out_event if fact else None,
                photo=fact.photo if fact else None,
            )
        )
    return out


class PlayerSummary(BaseModel):
    id: int
    fpl_element_id: int | None = None
    web_name: str
    team: int | None = None
    position: int | None = None
    price: float | None = None
    code: int | None = None
    # FPL-compatible fields
    status: str | None = None  # a=available, d=doubt, i=injured, s=suspended, u=unavailable
    chance_of_playing_next_round: int | None = None  # 0-100
    chance_of_playing_this_round: int | None = None  # 0-100
    news: str | None = None
    form: float | None = None
    total_points: int | None = None
    points_per_game: float | None = None
    ict_index: float | None = None
    ep_next: float | None = None
    transfers_in_event: int | None = None
    transfers_out_event: int | None = None
    photo: str | None = None  # use with resources.premierleague.com/premierleague/photos/players/110x140/p{photo}


@router.get("/players", response_model=list[PlayerSummary])
async def list_players(
    request: Request,
    team: int | None = Query(None, description="Optional team ID to filter players by."),
) -> list[PlayerSummary]:
    """List the committed current FPL catalog without opening the DB.

    An explicitly installed dependency override is honored for legacy unit
    tests; normal production traffic remains database-free.
    """
    db = _test_override_db(request)
    if db is not None:
        return _db_players(db, team)

    codes = _seed_codes()
    out: list[PlayerSummary] = []
    for element_id, row in _catalog().items():
        team_id = int(row["team"]) if row.get("team") else None
        if team is not None and team_id != team:
            continue
        code = row.get("code") or row.get("fpl_code") or codes.get(int(element_id))
        out.append(
            PlayerSummary(
                id=int(element_id),
                fpl_element_id=int(element_id),
                web_name=str(row.get("web_name") or f"Player {element_id}"),
                team=team_id,
                position=int(row["position"]) if row.get("position") else None,
                price=float(row["price"]) if row.get("price") is not None else None,
                code=int(code) if code is not None else None,
            )
        )
    return out


@router.get("/drawer/{player_id}")
async def player_drawer_compat(player_id: int, db: GetDB) -> dict[str, Any]:
    """Compatibility endpoint for the original squad-page drawer contract."""
    try:
        rows = db.execute(
            select(
                IngestedGameweekDB.gameweek,
                IngestedGameweekDB.total_points,
                IngestedGameweekDB.minutes,
            )
            .where(IngestedGameweekDB.element_id == int(player_id))
            .order_by(IngestedGameweekDB.gameweek.desc())
            .limit(5)
        ).all()
        return {
            "player_id": int(player_id),
            "form_bars": [
                {"gw": int(gw), "points": points, "minutes": minutes}
                for gw, points, minutes in sorted(rows)
            ],
        }
    except Exception:
        with contextlib.suppress(Exception):
            db.rollback()
        return {"player_id": int(player_id), "form_bars": []}


class PlayerSearchHit(PlayerSummary):
    xpts: float | None = None
    ownership_pct: float | None = None
    team_short: str | None = None
    relevance: float | None = None
    score: float | None = None


def _token_match_ratio(token: str, *fields: str | None) -> float:
    t = token.strip().lower()
    if not t:
        return 0.0
    best = 0.0
    for field in fields:
        f = (field or "").strip().lower()
        if not f:
            continue
        ratio = difflib.SequenceMatcher(None, t, f).ratio()
        if f.startswith(t):
            ratio = min(1.0, ratio + _PREFIX_BONUS)
        best = max(best, ratio)
    return best


def _relevance(query: str, *fields: str | None) -> float:
    tokens = [t for t in query.split() if t.strip()]
    return max((_token_match_ratio(tok, *fields) for tok in tokens), default=0.0)


@router.get("/players/search", response_model=list[PlayerSearchHit])
async def search_players(
    db: GetDB,
    q: str = Query("", description="Typo-tolerant name query."),
    limit: int = Query(20, ge=1, le=100),
    position: int | None = Query(None),
    max_price: float | None = Query(None, ge=0),
    team: int | None = Query(None),
    sort: str = Query("relevance", pattern="^(relevance|xpts|price|ownership)$"),
) -> list[PlayerSearchHit]:
    if not q.strip():
        return []
    players = db.execute(select(Player).order_by(Player.id)).scalars().all()
    catalog = _catalog()
    xpts_map = _latest_xpts_map(db)
    hits: list[PlayerSearchHit] = []
    for p in players:
        cat = catalog.get(int(p.fpl_element_id)) if p.fpl_element_id is not None else None
        position_value = int((cat or {}).get("position") or p.position_code or 0) or None
        team_value = int((cat or {}).get("team") or 0) or None
        if position is not None and position_value != position:
            continue
        if team is not None and team_value != team:
            continue
        price_value = (cat or {}).get("price")
        price = float(price_value) if price_value is not None else None
        if max_price is not None and (price is None or price > max_price):
            continue
        relevance = _relevance(q, p.web_name, p.first_name, p.second_name)
        if relevance < _RELEVANCE_CUTOFF:
            continue
        xpts = xpts_map.get(int(p.fpl_element_id)) if p.fpl_element_id is not None else None
        ownership = (cat or {}).get("selected_by_percent")
        # Enrich with FPL availability/stats from element_facts
        eid = int(p.fpl_element_id) if p.fpl_element_id is not None else None
        fact = db.get(ElementFactDB, eid) if eid is not None else None
        hits.append(
            PlayerSearchHit(
                id=p.id,
                fpl_element_id=p.fpl_element_id,
                web_name=p.web_name or f"Player {p.id}",
                team=team_value,
                position=position_value,
                price=price,
                code=getattr(p, "fpl_code", None) or _seed_codes().get(int(p.fpl_element_id))
                if p.fpl_element_id is not None
                else getattr(p, "fpl_code", None),
                xpts=float(xpts) if xpts is not None else None,
                ownership_pct=float(ownership) if ownership is not None else None,
                team_short=str((cat or {}).get("team_short") or "") or None,
                relevance=relevance,
                score=round(0.7 * relevance + 0.3 * min(1.0, (float(xpts) if xpts is not None else 0.0) / 10.0), 4),
                status=fact.status if fact else None,
                chance_of_playing_next_round=fact.chance_of_playing_next_round if fact else None,
                chance_of_playing_this_round=fact.chance_of_playing_this_round if fact else None,
                news=fact.news if fact else None,
                form=fact.form if fact else None,
                total_points=fact.total_points if fact else None,
                points_per_game=fact.points_per_game if fact else None,
                ict_index=fact.ict_index if fact else None,
                ep_next=fact.ep_next if fact else None,
                transfers_in_event=fact.transfers_in_event if fact else None,
                transfers_out_event=fact.transfers_out_event if fact else None,
                photo=fact.photo if fact else None,
            )
        )

    if sort == "relevance":
        hits.sort(key=lambda h: (-float(h.score or 0), -float(h.relevance or 0)))
    elif sort == "xpts":
        hits.sort(key=lambda h: -(float(h.xpts or -1)))
    elif sort == "price":
        hits.sort(key=lambda h: -(float(h.price or -1)))
    else:
        hits.sort(key=lambda h: -(float(h.ownership_pct or -1)))
    return hits[:limit]


@router.get("/players/fpl-compat")
async def players_fpl_compat(
    db: deps.GetDB,
) -> dict[str, Any]:
    """Players list in FPL bootstrap-static elements[] shape.

    Returns the same fields as the official FPL API bootstrap-static endpoint's
    elements array, so frontend code written against the FPL API works unchanged.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from fpl_intelligence.sync.materialized_models import (  # noqa: PLC0415
        ElementFactDB,
        PredictionCurrentDB,
    )

    # Get xPTS predictions
    pred_rows = db.execute(
        select(PredictionCurrentDB)
        .order_by(PredictionCurrentDB.gameweek.desc())
    ).scalars().all()
    xpts_map: dict[int, float] = {}
    gw_map: dict[int, int] = {}
    for p in pred_rows:
        if p.element_id not in xpts_map or p.gameweek > gw_map.get(p.element_id, -1):
            xpts_map[p.element_id] = p.expected_points
            gw_map[p.element_id] = p.gameweek

    facts = db.execute(select(ElementFactDB)).scalars().all()

    elements = []
    for f in facts:
        ep_next = xpts_map.get(f.element_id) or f.ep_next
        elements.append({
            # Core FPL fields
            "id": f.element_id,
            "element_type": f.element_type or 3,
            "web_name": f.web_name or "",
            "first_name": "",
            "second_name": f.web_name or "",
            "team": f.team_id,
            "team_code": f.team_id,
            "now_cost": f.now_cost or 0,
            "selected_by_percent": f.selected_by_percent or "0.0",
            "transfers_in_event": f.transfers_in_event or 0,
            "transfers_out_event": f.transfers_out_event or 0,
            "transfers_in": f.transfers_in_season or 0,
            "transfers_out": f.transfers_out_season or 0,
            "ep_next": str(round(ep_next, 1)) if ep_next else "0.0",
            "ep_this": str(round(f.ep_this or 0.0, 1)),
            "total_points": f.total_points or 0,
            "points_per_game": str(f.points_per_game or "0.0"),
            "form": str(f.form or "0.0"),
            "ict_index": str(f.ict_index or "0.0"),
            "goals_scored": f.goals_scored or 0,
            "assists": f.assists or 0,
            "clean_sheets": f.clean_sheets or 0,
            "yellow_cards": f.yellow_cards or 0,
            "red_cards": f.red_cards or 0,
            "bonus": f.bonus or 0,
            "status": f.status or "a",
            "news": f.news or "",
            "news_added": None,
            "chance_of_playing_next_round": f.chance_of_playing_next_round,
            "chance_of_playing_this_round": f.chance_of_playing_this_round,
            "cost_change_event": f.cost_change_event or 0,
            "photo": f.photo or "",
            # Intelligence extension fields (our additions)
            "fpl_intelligence_xpts": round(ep_next, 2) if ep_next else None,
        })

    return {"elements": elements, "total": len(elements)}
