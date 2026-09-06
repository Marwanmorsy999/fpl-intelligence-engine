"""Player discovery/search endpoints."""

from __future__ import annotations

import difflib
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.db.models import Player
from fpl_intelligence.sync.materialized_models import _latest_xpts_map

router = APIRouter()
GetDB = deps.GetDB

_RELEVANCE_CUTOFF = 0.45
_PREFIX_BONUS = 0.25
_catalog_cache: dict[int, dict[str, Any]] | None = None


def _catalog() -> dict[int, dict[str, Any]]:
    global _catalog_cache
    if _catalog_cache is None:
        from fpl_intelligence.prediction.live_provider import load_player_catalog
        _catalog_cache = load_player_catalog()
    return _catalog_cache


def _reset_catalog_cache() -> None:
    global _catalog_cache
    _catalog_cache = None


def _player_price(perf_price: float | None, fpl_element_id: int | None) -> float | None:
    if perf_price is not None:
        return float(perf_price)
    if fpl_element_id is None:
        return None
    row = _catalog().get(int(fpl_element_id))
    return float(row["price"]) if row and row.get("price") is not None else None


class PlayerSummary(BaseModel):
    id: int
    fpl_element_id: int | None = None
    web_name: str
    team: int | None = None
    position: int | None = None
    price: float | None = None
    code: int | None = None


@router.get("/players", response_model=list[PlayerSummary])
async def list_players(
    db: GetDB,
    team: int | None = Query(None, description="Optional team ID to filter players by."),
) -> list[PlayerSummary]:
    """List current FPL players with one compact identity query.

    Current team, position and price come from the immutable bootstrap catalog;
    this avoids scanning the large historical membership/performance tables on
    every dashboard load.
    """
    players = db.execute(select(Player).order_by(Player.id)).scalars().all()
    catalog = _catalog()
    out: list[PlayerSummary] = []
    for p in players:
        row = catalog.get(int(p.fpl_element_id)) if p.fpl_element_id is not None else None
        team_id = int(row["team"]) if row and row.get("team") else None
        if team is not None and team_id != team:
            continue
        out.append(
            PlayerSummary(
                id=p.id,
                fpl_element_id=p.fpl_element_id,
                web_name=p.web_name or (row or {}).get("web_name", f"Player {p.id}"),
                team=team_id,
                position=(int(row["position"]) if row and row.get("position") else p.position_code),
                price=(float(row["price"]) if row and row.get("price") is not None else None),
                code=p.fpl_code,
            )
        )
    return out


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
        price_value = _player_price(None, p.fpl_element_id)
        if position is not None and position_value != position:
            continue
        if team is not None and team_value != team:
            continue
        if max_price is not None and (price_value is None or price_value > max_price):
            continue
        relevance = _relevance(q, p.web_name, " ".join(filter(None, (p.first_name, p.second_name))), (cat or {}).get("web_name"))
        if relevance < _RELEVANCE_CUTOFF:
            continue
        xpts = xpts_map.get(p.fpl_element_id) if p.fpl_element_id is not None else None
        score = round(0.7 * relevance + 0.3 * min(1.0, (xpts or 0.0) / 10.0), 4)
        hits.append(PlayerSearchHit(
            id=p.id,
            fpl_element_id=p.fpl_element_id,
            web_name=p.web_name or (cat or {}).get("web_name", f"Player {p.id}"),
            team=team_value,
            position=position_value,
            price=price_value,
            code=p.fpl_code,
            xpts=xpts,
            ownership_pct=(cat or {}).get("selected_by_percent"),
            team_short=(cat or {}).get("team_short"),
            relevance=round(relevance, 4),
            score=score,
        ))
    if sort == "xpts":
        hits.sort(key=lambda h: (h.xpts is None, -(h.xpts or 0.0)))
    elif sort == "price":
        hits.sort(key=lambda h: (h.price is None, -(h.price or 0.0)))
    elif sort == "ownership":
        hits.sort(key=lambda h: (h.ownership_pct is None, -(h.ownership_pct or 0.0)))
    else:
        hits.sort(key=lambda h: -(h.score or 0.0))
    return hits[:limit]
