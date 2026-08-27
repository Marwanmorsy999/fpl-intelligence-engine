"""Phase 11.3 — Player browser endpoints.

A small DX helper so API users can discover valid integer player IDs (with
their team, position, and current price) to feed into ``POST /api/v1/squad``.
Players are read from the ingested database.

Pass 2 (2026-08-27) improvements
--------------------------------
* Team and price are resolved with TWO batched queries total instead of one
  query per player (~1,200 queries → 2 for a 600-player table).
* ``GET /players`` falls back to the bootstrap catalog price when a player
  has no gameweek performance row yet (kills the early-season "£—").
* ``GET /players/search`` — typo-tolerant player search with an xPTS-aware
  relevance score, filters (position / max_price / team) and sorts
  (relevance / xpts / price / ownership).
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.db.models import (
    Player,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
)
from fpl_intelligence.sync.materialized_models import _latest_xpts_map

router = APIRouter()
logger = logging.getLogger(__name__)

GetDB = deps.GetDB

#: Relevance floor — below this ratio a hit is noise, not a typo.
_RELEVANCE_CUTOFF = 0.45
#: Bonus added when a name field starts with the query token (capped at 1.0).
_PREFIX_BONUS = 0.25

#: Bootstrap catalog cache — the committed seed never changes at runtime, so
#: one load per process is enough. Tests reset it via _reset_catalog_cache().
_catalog_cache: dict[int, dict[str, Any]] | None = None


def _catalog() -> dict[int, dict[str, Any]]:
    """Process-cached bootstrap catalog (element_id -> row)."""
    global _catalog_cache
    if _catalog_cache is None:
        # Imported lazily so tests can monkeypatch
        # fpl_intelligence.prediction.live_provider.load_player_catalog.
        from fpl_intelligence.prediction.live_provider import load_player_catalog

        _catalog_cache = load_player_catalog()
    return _catalog_cache


def _reset_catalog_cache() -> None:
    """Tests only: drop the process-cached catalog."""
    global _catalog_cache
    _catalog_cache = None


def _latest_team_map(db: Session) -> dict[int, int | None]:
    """player_id -> team_id of the LATEST membership, in ONE query.

    Rows are read newest-first by surrogate id; the first occurrence per
    player is the current membership. Replaces the per-player lookups.
    """
    rows = db.execute(
        select(PlayerTeamMembership.player_id, PlayerTeamMembership.team_id).order_by(
            PlayerTeamMembership.id.desc()
        )
    ).all()
    latest: dict[int, int | None] = {}
    for player_id, team_id in rows:
        latest.setdefault(player_id, team_id)
    return latest


def _latest_price_map(db: Session) -> dict[int, float | None]:
    """player_id -> LATEST non-null price, in ONE query.

    Gameweek snapshots are read newest-first; the first non-null price per
    player wins. Replaces the per-player lookups.
    """
    rows = db.execute(
        select(PlayerGameweekPerformance.player_id, PlayerGameweekPerformance.price)
        .where(PlayerGameweekPerformance.price.is_not(None))
        .order_by(PlayerGameweekPerformance.gameweek_id.desc())
    ).all()
    latest: dict[int, float | None] = {}
    for player_id, price in rows:
        latest.setdefault(player_id, price)
    return latest


def _player_price(perf_price: float | None, fpl_element_id: int | None) -> float | None:
    """Gameweek price first; bootstrap catalog fallback (kills early £—).

    Players whose ``fpl_element_id`` is NULL stay honestly ``null`` — there is
    no catalog key to fall back to, and we never invent a price.
    """
    if perf_price is not None:
        return perf_price
    if fpl_element_id is None:
        return None
    row = _catalog().get(fpl_element_id)
    if row is not None and row.get("price"):
        return row["price"]
    return None


class PlayerSummary(BaseModel):
    """Compact view of an ingested player for squad-building.

    ``fpl_element_id`` is the canonical, single-ID-space identifier the squad
    engine uses everywhere (R1). The frontend picks by this value; when it is
    ``null`` (a legacy row not yet linked to FPL) the internal ``id`` is used
    as a fallback.
    """

    id: int
    fpl_element_id: int | None = None
    web_name: str
    team: int | None = None
    position: int | None = None
    price: float | None = None
    #: FPL element code used for Premier-League-CDN photo URLs. May be ``null``
    #: when the player was seeded without a code (falls back to initials avatar).
    code: int | None = None


@router.get("/players", response_model=list[PlayerSummary])
async def list_players(
    db: GetDB,
    team: int | None = Query(None, description="Optional team ID to filter players by."),
) -> list[PlayerSummary]:
    """List ingested players so callers can find valid IDs for the squad endpoint.

    Returns each player's ``id``, ``web_name``, current ``team`` (team_id),
    ``position`` (position_code: 1=GK, 2=DEF, 3=MID, 4=FWD), and latest ``price``
    in millions. Team and price are best-effort: they are ``null`` when no
    membership / gameweek performance has been ingested for the player —
    except price, which falls back to the bootstrap catalog when the element
    is linked to FPL (early-season rows without any performance yet).
    """
    query = select(Player)
    if team is not None:
        subq = select(PlayerTeamMembership.player_id).where(PlayerTeamMembership.team_id == team)
        query = query.where(Player.id.in_(subq))

    players = db.execute(query.order_by(Player.id)).scalars().all()

    team_map = _latest_team_map(db)
    price_map = _latest_price_map(db)

    return [
        PlayerSummary(
            id=p.id,
            fpl_element_id=p.fpl_element_id,
            web_name=p.web_name,
            team=team_map.get(p.id),
            position=p.position_code,
            price=_player_price(price_map.get(p.id), p.fpl_element_id),
            code=p.fpl_code,
        )
        for p in players
    ]


class PlayerSearchHit(PlayerSummary):
    """A search hit enriched with xPTS, ownership and the blended score."""

    #: xPTS from the newest-gameweek ``predictions_current`` row (null when
    #: the element has no precomputed row for that gameweek).
    xpts: float | None = None
    #: Catalog selected-by share (ownership %). Null when unlisted.
    ownership_pct: float | None = None
    #: Catalog team short name (photo/label convenience).
    team_short: str | None = None
    #: Typo-tolerant name match ratio in [0, 1], 4 dp.
    relevance: float | None = None
    #: 0.7 * relevance + 0.3 * min(1, xpts/10), rounded to 4 dp.
    score: float | None = None


def _token_match_ratio(token: str, *fields: str | None) -> float:
    """Best difflib ratio of one query token across the given name fields.

    A field that STARTS WITH the token earns a prefix bonus (capped at 1.0) —
    that is how "sal" still finds "Salah" even though the sequences are short.
    """
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
    """Typo-tolerant relevance: max over query tokens, max over fields."""
    tokens = [t for t in query.split() if t.strip()]
    if not tokens:
        return 0.0
    return max(_token_match_ratio(tok, *fields) for tok in tokens)


@router.get("/players/search", response_model=list[PlayerSearchHit])
async def search_players(
    db: GetDB,
    q: str = Query("", description="Typo-tolerant name query (space-separated tokens)."),
    limit: int = Query(20, ge=1, le=100, description="Max hits to return."),
    position: int | None = Query(None, description="position_code: 1=GK 2=DEF 3=MID 4=FWD"),
    max_price: float | None = Query(None, ge=0, description="Price cap in millions."),
    team: int | None = Query(None, description="Filter by (latest) team id."),
    sort: str = Query(
        "relevance",
        pattern="^(relevance|xpts|price|ownership)$",
        description="relevance (score blend) | xpts | price | ownership",
    ),
) -> list[PlayerSearchHit]:
    """Typo-tolerant player search for the squad builder.

    * Match: per-token difflib ratio vs ``web_name``, ``first second`` and the
      catalog full name, +0.25 prefix bonus, max over tokens and fields.
      Hits below 0.45 relevance are dropped (noise, not a typo).
    * Score: ``round(0.7 * relevance + 0.3 * min(1, xpts/10), 4)`` — xPTS come
      from the newest-gameweek ``predictions_current`` rows.
    * Each hit is enriched with ``xpts``, ``ownership_pct`` and ``team_short``
      from the bootstrap catalog.
    """
    if not q.strip():
        return []

    players = db.execute(select(Player).order_by(Player.id)).scalars().all()
    team_map = _latest_team_map(db)
    price_map = _latest_price_map(db)
    xpts_map = _latest_xpts_map(db)
    catalog = _catalog()

    hits: list[PlayerSearchHit] = []
    for p in players:
        price = _player_price(price_map.get(p.id), p.fpl_element_id)
        team_id = team_map.get(p.id)

        # Filters (an explicit max_price excludes unpriced rows: we cannot
        # prove they are under the cap, so they stay out honestly).
        if position is not None and p.position_code != position:
            continue
        if team is not None and team_id != team:
            continue
        if max_price is not None and price is None:
            continue
        if max_price is not None and price > max_price:
            continue

        cat = catalog.get(p.fpl_element_id) if p.fpl_element_id is not None else None
        first_second = " ".join(filter(None, (p.first_name, p.second_name)))
        relevance = _relevance(q, p.web_name, first_second, (cat or {}).get("web_name"))
        if relevance < _RELEVANCE_CUTOFF:
            continue

        xpts = xpts_map.get(p.fpl_element_id) if p.fpl_element_id is not None else None
        xpts_norm = min(1.0, (xpts or 0.0) / 10.0)
        score = round(0.7 * relevance + 0.3 * xpts_norm, 4)
        hits.append(
            PlayerSearchHit(
                id=p.id,
                fpl_element_id=p.fpl_element_id,
                web_name=p.web_name,
                team=team_id,
                position=p.position_code,
                price=price,
                code=p.fpl_code,
                xpts=xpts,
                ownership_pct=(cat or {}).get("selected_by_percent"),
                team_short=(cat or {}).get("team_short") or None,
                relevance=round(relevance, 4),
                score=score,
            )
        )

    if sort == "xpts":
        hits.sort(key=lambda h: (h.xpts is None, -(h.xpts or 0.0)))
    elif sort == "price":
        hits.sort(key=lambda h: (h.price is None, -(h.price or 0.0)))
    elif sort == "ownership":
        hits.sort(key=lambda h: (h.ownership_pct is None, -(h.ownership_pct or 0.0)))
    else:  # relevance — the blended score
        hits.sort(key=lambda h: (-(h.score or 0.0)))

    return hits[:limit]
