"""Phase 19.0 — machine-to-machine sync endpoints.

Three push routes (bookmarklet, Google Apps Script fetcher, GitHub Actions)
guarded by ``Authorization: Bearer <SYNC_PUSH_TOKEN>``, plus the public read
models they feed: track record, live board, sync status and crests.

Auth contract
-------------
* token unset in config  -> 503 (an unconfigured deployment accepts nothing),
* wrong/missing bearer   -> 401,
* correct bearer         -> proceed. Comparison is constant-time.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from fpl_intelligence.api import deps
from fpl_intelligence.config import get_settings
from fpl_intelligence.squad.models import SquadStateCreate, SquadStateResponse
from fpl_intelligence.squad.service import SquadService
from fpl_intelligence.sync.models import SyncLogDB
from fpl_intelligence.sync.service import (
    calibration_snapshot,
    ingest_history_gameweek,
    save_live_points,
    track_record_payload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync", tags=["sync"])

GetDB = deps.GetDB


def _require_push_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Bearer-token gate shared by all three push endpoints."""
    expected = get_settings().sync_push_token.strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="SYNC_PUSH_TOKEN not configured on the server; pushes are disabled.",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    supplied = authorization[len("Bearer "):]
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


# --------------------------------------------------------------------------- #
# Push payloads
# --------------------------------------------------------------------------- #


class PickItem(BaseModel):
    model_config = ConfigDict(extra="allow")  # element_type rides along from FPL

    element_id: int = Field(..., gt=0)
    position: int = Field(..., ge=1, le=15, description="1-11 starters, 12-15 bench")
    is_captain: bool = False
    is_vice: bool = False


class SquadPushPayload(BaseModel):
    entry_id: int = Field(..., gt=0)
    entry_name: str | None = None
    gameweek: int = Field(..., gt=0)
    picks: list[PickItem] = Field(..., min_length=15, max_length=15)
    bank: float = 0.0
    transfers: dict[str, Any] | None = None


class LivePushPayload(BaseModel):
    gameweek: int = Field(..., gt=0)
    elements: list[dict[str, Any]] = Field(..., min_length=1)


class HistoryPushPayload(BaseModel):
    gameweek: int = Field(..., gt=0)
    source: str = "github-actions"
    season: str | None = None
    elements: list[dict[str, Any]] = Field(..., min_length=1)


def _log_sync(
    db: Any,
    kind: str,
    *,
    entry_id: str | None,
    gameweek: int | None,
    detail: dict,
) -> None:
    db.add(
        SyncLogDB(
            kind=kind,
            entry_id=entry_id,
            gameweek=gameweek,
            ok=True,
            detail=detail,
            created_at=datetime.now(UTC),
        )
    )


# --------------------------------------------------------------------------- #
# POST /sync/squad-push — bookmarklet + Apps Script daily trigger
# --------------------------------------------------------------------------- #


@router.post("/squad-push", dependencies=[Depends(_require_push_auth)])
async def squad_push(payload: SquadPushPayload, db: GetDB) -> dict[str, Any]:
    """Persist a synced squad under session key = entry_id.

    Converts FPL pick positions into the internal squad representation so
    /api/v1/decisions works immediately afterwards. Position codes come from
    FPL's element_type on the client side when available (picks carry
    ``element_type``), otherwise the engine resolves them from its player DB.
    """
    ids: list[int] = []
    captain_id = vice_id = 0
    positions: dict[int, int] = {}
    for i, pick in enumerate(payload.picks):
        ids.append(pick.element_id)
        if pick.is_captain and captain_id == 0:
            captain_id = pick.element_id
        if pick.is_vice and vice_id == 0:
            vice_id = pick.element_id
        et = payload.picks[i].model_extra or {}
        element_type = et.get("element_type") or et.get("position_code")
        if isinstance(element_type, int):
            positions[pick.element_id] = element_type

    if captain_id == 0:
        raise HTTPException(status_code=422, detail="payload must mark exactly one is_captain pick")

    squad = SquadStateCreate(
        player_ids=ids,
        captain_id=captain_id,
        vice_captain_id=vice_id if vice_id else ids[0],
        bank=payload.bank,
        free_transfers=_free_transfers_from(payload.transfers),
        chips_available=["wildcard", "free_hit", "bench_boost", "triple_captain"],
        gameweek=payload.gameweek,
        player_positions=positions or None,
        player_prices=None,
        player_teams=None,
        session_id=str(payload.entry_id),
    )
    saved: SquadStateResponse = SquadService(session=db).set_squad(
        squad, session_id=str(payload.entry_id)
    )
    _log_sync(
        db,
        "squad",
        entry_id=str(payload.entry_id),
        gameweek=payload.gameweek,
        detail={
            "entry_name": payload.entry_name,
            "players": len(ids),
            "bank": payload.bank,
            "transfers": payload.transfers,
        },
    )
    db.commit()
    return {
        "ok": True,
        "session_id": str(payload.entry_id),
        "entry_name": payload.entry_name,
        "gameweek": payload.gameweek,
        "players": len(saved.player_ids),
        "captain": saved.captain_id,
    }


def _free_transfers_from(transfers: dict[str, Any] | None) -> int:
    if not isinstance(transfers, dict):
        return 1
    limit = transfers.get("limit")
    made = transfers.get("made", transfers.get("made_transfers"))
    try:
        free = max(0, int(limit) - max(0, int(made or 0)))
    except (TypeError, ValueError):
        return 1
    return min(free, 5)


# --------------------------------------------------------------------------- #
# POST /sync/live-push — Apps Script matchday trigger
# --------------------------------------------------------------------------- #


@router.post("/live-push", dependencies=[Depends(_require_push_auth)])
async def live_push(payload: LivePushPayload, db: GetDB) -> dict[str, Any]:
    """Upsert per-player live points for a matchday gameweek."""
    result = save_live_points(db, payload.gameweek, payload.elements)
    _log_sync(db, "live", entry_id=None, gameweek=payload.gameweek, detail=result)
    db.commit()
    return {"ok": True, "gameweek": payload.gameweek, **result}


# --------------------------------------------------------------------------- #
# POST /sync/history-push — GitHub Actions vaastav/Understat refresh
# --------------------------------------------------------------------------- #


@router.post("/history-push", dependencies=[Depends(_require_push_auth)])
async def history_push(payload: HistoryPushPayload, db: GetDB) -> dict[str, Any]:
    """Ingest a vaastav-format gameweek and rebuild derived math.

    Order of operations inside :func:`ingest_history_gameweek` guarantees the
    pre-match baseline forecast is captured BEFORE new rows change the form
    window, then actuals fill the ledger, pending recommendations auto-score,
    and the calibration snapshot recomputes.
    """
    result = ingest_history_gameweek(
        db, payload.gameweek, payload.elements, source=payload.source
    )
    _log_sync(
        db,
        "history",
        entry_id=None,
        gameweek=payload.gameweek,
        detail={"stored": result["stored"], "mirrored": result["mirrored"]},
    )
    db.commit()
    return {"ok": True, **result}


# --------------------------------------------------------------------------- #
# Public read models
# --------------------------------------------------------------------------- #


@router.get("/status")
async def sync_status(db: GetDB) -> dict[str, Any]:
    """Last successful push per kind (public read; no secrets returned)."""
    from sqlalchemy import select

    rows = (
        db.execute(select(SyncLogDB).order_by(SyncLogDB.created_at.desc()).limit(200)).scalars().all()
    )
    latest: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for row in reversed(rows):  # oldest-first so newest overwrites
        counts[row.kind] = counts.get(row.kind, 0) + 1
        if row.ok:
            latest[row.kind] = {
                "at": row.created_at.isoformat() if row.created_at else None,
                "entry_id": row.entry_id,
                "gameweek": row.gameweek,
                "detail": row.detail,
            }
    return {
        "latest": latest,
        "counts": counts,
        "token_configured": bool(get_settings().sync_push_token.strip()),
    }


@router.get("/track-record")
async def track_record(
    db: GetDB,
    entry_id: Annotated[str, Query(description="FPL entry id (= session key)")],
) -> dict[str, Any]:
    """Every saved recommendation with verdicts, hit-rate and last-5 cards."""
    return track_record_payload(db, entry_id)


@router.get("/calibration")
async def calibration(db: GetDB) -> dict[str, Any]:
    """Predicted-vs-actual aggregate across the whole ledger."""
    return calibration_snapshot(db)


@router.get("/live-board")
async def live_board(
    db: GetDB,
    session_id: Annotated[str, Query(description="Entry id whose squad to show")],
    gameweek: int | None = Query(None, description="Defaults to the saved squad's GW"),
) -> dict[str, Any]:
    """Matchday board: the user's picks with live points pushed by Apps Script.

    Honest contract: rows only appear for players with actually-pushed live
    data. When nothing has been pushed yet the payload says so and points the
    user at the ESPN scoreboard cross-check — never fabricated zeros.
    """
    from sqlalchemy import select

    from fpl_intelligence.db.models import Player
    from fpl_intelligence.sync.models import SyncLivePointDB

    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    gw = gameweek or squad.gameweek
    live_rows = db.execute(
        select(SyncLivePointDB).where(SyncLivePointDB.gameweek == gw)
    ).scalars().all()
    live_by_element = {r.element_id: r for r in live_rows}

    names: dict[int, str] = {}
    for pid in squad.player_ids:
        player = db.scalar(select(Player).where(Player.fpl_element_id == pid))
        names[pid] = player.web_name if player else f"Player {pid}"

    def _row(pid: int, *, on_bench: bool = False) -> dict[str, Any]:
        live = live_by_element.get(pid)
        return {
            "element_id": pid,
            "name": names.get(pid),
            "on_bench": on_bench,
            "is_captain": pid == squad.captain_id,
            "live_points": (live.points if live else None),
            "minutes": (live.minutes if live else None),
            "fixture": (live.fixture_text if live else None),
            "opponent": (live.opponent if live else None),
            "updated_at": (live.updated_at.isoformat() if live and live.updated_at else None),
        }

    # squad-push preserves FPL pick-slot order: slots 1-11 start, 12-15 bench.
    starters = squad.player_ids[:11]
    bench = squad.player_ids[11:]
    rows = [_row(p) for p in starters] + [_row(p, on_bench=True) for p in bench]
    total = sum(r["live_points"] or 0 for r in rows if not r["on_bench"])
    captain_row = next((r for r in rows if r["is_captain"]), None)
    effective_total = total
    if captain_row and captain_row["live_points"] is not None:
        effective_total += captain_row["live_points"]  # armband doubles it
    return {
        "session_id": session_id,
        "gameweek": gw,
        "rows": rows,
        "total_live_points": total,
        "effective_total": effective_total,
        "players_with_data": len(live_by_element),
        "has_live_data": bool(live_by_element),
        "espn_fallback_url": "https://www.espn.com/soccer/scoreboard/_/league/eng.1",
        "note": (
            None
            if live_by_element
            else "No live data pushed yet for this gameweek. The Apps Script "
            "matchday trigger pushes every hour 14:00-18:00 UTC Sat/Sun; "
            "meanwhile cross-check scores on ESPN."
        ),
    }
