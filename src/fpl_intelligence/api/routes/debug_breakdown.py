"""Temp debug for v2.3.2 breakdown"""
from fastapi import APIRouter, Query
from fpl_intelligence.api import deps

router = APIRouter(prefix="/debug", tags=["debug"])

@router.get("/breakdown/{player_id}")
async def debug_breakdown(player_id: int, db: deps.GetDB, gw: int = Query(1)):
    from fpl_intelligence.prediction.live_provider import LivePredictionProvider
    provider = LivePredictionProvider(session=db)
    chain = provider.resolve_chain(int(gw), skip_materialized=True)
    out = []
    for lvl in chain.levels:
        per = lvl.per_player.get(int(player_id))
        out.append({"source": lvl.source, "per_player": per, "points": lvl.points.get(int(player_id)), "notes": lvl.notes})
    return {"levels": out, "materialized_skip": True, "player_id": player_id, "gw": gw}

@router.get("/materialized/{player_id}")
async def debug_materialized(player_id: int, db: deps.GetDB, gw: int = Query(1)):
    from sqlalchemy import select
    from fpl_intelligence.sync.materialized_models import PredictionCurrentDB
    row = db.execute(select(PredictionCurrentDB).where(PredictionCurrentDB.gameweek==int(gw), PredictionCurrentDB.element_id==int(player_id))).scalars().first()
    if row is None:
        return {"found": False}
    return {"found": True, "expected_points": row.expected_points, "breakdown": row.breakdown, "source": row.source, "computed_at": str(row.computed_at)}
