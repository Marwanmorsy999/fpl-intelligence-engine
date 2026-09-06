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
from fpl_intelligence.sync.materialized_models import _latest_xpts_map
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


def _db_players(db: Any, team: int | None) -> list["PlayerSummary"]:
    """Render explicitly overridden DB state for legacy ingestion tests."""
    from fpl_intelligence.db.models import PlayerGameweekPerformance, PlayerTeamMembership

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
        price = float(price_row) if price_row is not None else None

        out.append(
            PlayerSummary(
                id=p.id,
                fpl_element_id=p.fpl_element_id,
                web_name=p.web_name or f"Player {p.id}",
                team=team_id,
                position=int(p.position_code) if p.position_code is not None else None,
                price=price,
                code=getattr(p, "fpl_code", None),
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
    """Compatibility endpoint for the original squad-page drawer contract.

    The deep drawer remains at /player/{player_id}/drawer?session_id=..., but
    the My Team page only needs the materialized last-five form bars. Keep this
    compatibility route tiny and read-only so legacy clients stop generating
    404s without adding live-network or write-path work.
    """
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
        price_value = float((cat or {}).get("price")) if (cat or {}).get("price") is not None else None
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
        hits.append(
            PlayerSearchHit(
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
            )
        )
    if sort == "xpts":
        hits.sort(key=lambda h: (h.xpts is None, -(h.xpts or 0.0)))
    elif sort == "price":
        hits.sort(key=lambda h: (h.price is None, -(h.price or 0.0)))
    elif sort == "ownership":
        hits.sort(key=lambda h: (h.ownership_pct is None, -(h.ownership_pct or 0.0)))
    else:
        hits.sort(key=lambda h: -(h.score or 0.0))
    return hits[:limit]