"""Phase 10.4 — Squad Decision Engine REST endpoints.

Phase 11.1 extends ``GET /api/v1/decisions`` so it can *optionally* apply live
structured-API fact overrides (official FPL chance-of-playing, API-Football
confirmed lineups, etc.) before running the Phase 6 optimizers. When live facts
are unavailable — network failure, missing key, or ``live_facts=false`` — the
request falls back to the baseline quantitative predictions and still succeeds.
No API key is hardcoded; live calls (if any) are cache-first and never fail the
request.

Phase 11.2 persists the squad state to PostgreSQL: each request binds a
:class:`~fpl_intelligence.squad.service.SquadService` to the request's database
session, so the squad survives restarts and is shared across workers.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.config import get_settings
from fpl_intelligence.data_providers.decision_bridge import (
    FactCollectionService,
    FactOverrideProvider,
)
from fpl_intelligence.data_providers.understat import (
    UnderstatConnector,
    build_stats_from_row,
)
from fpl_intelligence.db.models import Player, PlayerExternalId
from fpl_intelligence.optimization.provider import DecisionPredictionProvider
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
from fpl_intelligence.squad.demo import build_demo_squad
from fpl_intelligence.squad.fpl_import import (
    FplApiUnavailable,
    FplEntryNotFound,
    FplPicksNotSaved,
    FplRateLimitBlocked,
    FplSquadImporter,
)
from fpl_intelligence.squad.models import (
    DecisionReport,
    FromFplResponse,
    PlayerDetail,
    SquadStateCreate,
    SquadStateResponse,
)
from fpl_intelligence.squad.service import SquadService
from fpl_intelligence.squad.sync import (
    NoPendingSync,
    get_pending_sync,
    run_pending_sync,
    save_pending_sync,
)

logger = logging.getLogger(__name__)

router = APIRouter()

GetDB = deps.GetDB

# --------------------------------------------------------------------------- #
# Rate limiting for the public retry-sync endpoint (Phase 13.5).
# A simple in-memory fixed-window limiter keyed by client IP. On a serverless
# runtime this is per-instance (adequate for a "don't hammer FPL on deadline
# day" guard); it is purely advisory and test-friendly.
# --------------------------------------------------------------------------- #
_retry_sync_lock = threading.Lock()
_retry_sync_stamps: dict[str, list[float]] = {}


def _retry_sync_rate_limited(host: str, limit: int, window: float) -> bool:
    """Return True when the caller has exceeded ``limit`` calls in ``window``."""
    now = time.monotonic()
    with _retry_sync_lock:
        recent = [t for t in _retry_sync_stamps.get(host, []) if now - t < window]
        if len(recent) >= limit:
            return True
        recent.append(now)
        _retry_sync_stamps[host] = recent
        return False


@router.post("/squad", response_model=SquadStateResponse, status_code=200)
async def set_squad(
    payload: SquadStateCreate, db: GetDB, response: Response, session_id: str | None = Query(None)
) -> SquadStateResponse:
    """Persist the user's FPL squad state.

    ``session_id`` query param isolates this squad to a per-user key
    (e.g. the FPL entry_id). When omitted the payload's ``session_id`` is used.
    """
    key = session_id or payload.session_id
    if not key:
        raise HTTPException(
            status_code=400,
            detail="session_id is required (query param or body field).",
        )
    result = SquadService(session=db).set_squad(payload, session_id=key)
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/squad",
    response_model=SquadStateResponse,
    responses={404: {"description": "No squad saved for this session"}},
)
async def get_squad(
    db: GetDB,
    response: Response,
    session_id: str | None = Query(None, description="Per-user session key. Required.")
) -> SquadStateResponse:
    """Retrieve the squad state for a specific session.

    ``session_id`` is REQUIRED. Returns 404 if missing or if no squad has been
    saved for that key — never falls back to a default or another user's squad.
    """
    if not session_id:
        raise HTTPException(
            status_code=404,
            detail="No squad saved for this session",
        )
    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(
            status_code=404,
            detail="No squad saved for this session",
        )
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    return squad


def _build_player_details(
    db: Session,
    report: DecisionReport,
    squad: SquadStateResponse,
    provider: DecisionPredictionProvider,
    understat_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, PlayerDetail]:
    """Enrich the decision report with per-player details for the dashboard.

    Looks up ``web_name``, ``team``, ``position``, ``price``, ``code``, and
    expected points (xPTS) for every player referenced in the report so the
    frontend can render photos, badges, and prices without a separate API
    round-trip.

    When the resolved provider is a :class:`LivePredictionProvider`, each
    prediction carries its ``source`` / ``data_quality`` labels plus
    ``expected_minutes`` and ``start_probability`` — all surfaced here so the
    UI can never present a heuristic proxy as a computed model output.

    ``understat_index`` is the live provider's name-keyed Understat snapshot
    index (passed in from the route, which holds the base provider, so this
    works whether or not the provider is wrapped in a
    :class:`FactOverrideProvider`). Players matched in the index get real
    xg/xa_per_90 values; everyone else gets ``None`` and the dashboard must
    NOT render xG/xA lines for them.
    """
    uindex = understat_index or {}

    # --- collect every player id referenced in the report --------------------
    player_ids: set[int] = set()
    player_ids.update(report.starting_xi)
    player_ids.update(report.bench_order)
    if report.captain is not None:
        player_ids.add(report.captain.player_id)
    if report.vice_captain is not None:
        player_ids.add(report.vice_captain)
    if report.transfer_plan is not None:
        player_ids.update(report.transfer_plan.transfers_in)
        player_ids.update(report.transfer_plan.transfers_out)

    # --- batch-fetch xPTS predictions from the provider ----------------------
    try:
        predictions = provider.get_squad_predictions(
            list(player_ids), [report.gameweek]
        )
    except Exception:  # noqa: BLE001 - xPTS is best-effort, never break the request
        predictions = {}
    gw_preds = predictions.get(report.gameweek, {})

    details: dict[str, PlayerDetail] = {}
    for pid in sorted(player_ids):
        # Resolve the canonical Player row for this id.
        #
        # Imported squads (one-click FPL flow) store OFFICIAL FPL ELEMENT ids as
        # player_ids, so they MUST be joined via ``players.fpl_element_id`` —
        # never against our internal auto-increment ``id`` (a different
        # keyspace: element 445 = Haaland used to resolve to whatever internal
        # row had id == 445, showing the wrong name under the right xPTS).
        # Demo squads store internal DB ids and take the ``db.get`` path below.
        player: Player | None = None
        if not squad.is_demo:
            player = db.scalar(select(Player).where(Player.fpl_element_id == pid))
            if player is None:
                # Legacy fallback for databases seeded before migration 0016:
                # resolve through the external-id mapping table.
                for ext_provider in ("official_fpl", "fpl"):
                    ext = db.scalar(
                        select(PlayerExternalId).where(
                            PlayerExternalId.provider == ext_provider,
                            PlayerExternalId.provider_player_id == str(pid),
                        )
                    )
                    if ext is not None:
                        player = db.get(Player, ext.player_id)
                        break
        if player is None:
            # Demo squads (and manual squads built from GET /api/v1/players ids)
            # use internal DB ids.
            player = db.get(Player, pid)

        # --- assemble PlayerDetail ------------------------------------------
        if player is not None:
            web_name = player.web_name
            position = player.position_code
            code = player.fpl_code
        else:
            web_name = f"Player {pid}"
            position = None
            # Plausible fallback for the PL photo URL; onerror handles 404s.
            code = pid

        # Squad metadata (from FPL bootstrap) is the authoritative source for
        # team/price/position when available — it is current and complete.
        team = squad.player_teams.get(pid) if squad.player_teams else None
        price = squad.player_prices.get(pid) if squad.player_prices else None
        if position is None and squad.player_positions:
            position = squad.player_positions.get(pid)

        pred = gw_preds.get(pid)
        if pred is not None:
            expected_points = round(pred.expected_points, 2)
            # LabeledPlayerPrediction carries source/data_quality; plain
            # PlayerPrediction (e.g. after a FactOverride) does not — guard.
            prediction_source = getattr(pred, "source", None) or None
            data_quality = getattr(pred, "data_quality", None) or None
            minutes_estimate = (
                round(float(pred.expected_minutes), 1)
                if pred.expected_minutes is not None
                else None
            )
            start_prob = (
                round(float(pred.start_probability), 3)
                if pred.start_probability is not None
                else None
            )
        else:
            expected_points = None
            prediction_source = None
            data_quality = None
            minutes_estimate = None
            start_prob = None

        # Understat xG/xA: ONLY for genuinely matched players, never fabricated.
        xg = None
        xa = None
        if uindex and web_name:
            row = UnderstatConnector.match_player(uindex, web_name)
            if row is not None:
                try:
                    stats = build_stats_from_row(row)
                    xg = round(float(stats.xg_per_90), 2)
                    xa = round(float(stats.xa_per_90), 2)
                except Exception:  # noqa: BLE001 — skip unparseable rows
                    pass

        details[str(pid)] = PlayerDetail(
            id=pid,
            web_name=web_name,
            team=team,
            position=position,
            price=price,
            code=code,
            expected_points=expected_points,
            prediction_source=prediction_source,
            data_quality=data_quality,
            minutes_estimate=minutes_estimate,
            start_prob=start_prob,
            xg=xg,
            xa=xa,
        )

    return details


@router.get(
    "/decisions",
    response_model=DecisionReport,
    responses={404: {"description": "No squad saved for this session"}},
)
async def get_decisions(
    db: GetDB,
    response: Response,
    provider: Annotated[DecisionPredictionProvider, Depends(deps.get_prediction_provider)],
    live_facts: bool = Query(
        False,
        description="Apply live structured-API fact overrides before optimizing.",
    ),
    session_id: str | None = Query(
        None,
        description="Per-user session key. REQUIRED. Reads the squad row for this session.",
    ),
) -> DecisionReport:
    """Generate a personalized :class:`DecisionReport` for the stored squad.

    ``session_id`` is REQUIRED. Returns 404 if missing or if no squad has been
    saved for that key — never falls back to a default or another user's squad.

    When ``live_facts=true`` the engine attempts to fetch hard facts from the
    official FPL API (and any keyed provider that is enabled) and override the
    baseline predictions accordingly. If live facts cannot be obtained the
    request degrades gracefully to the baseline quantitative predictions and
    still succeeds — it never fails because of an upstream API problem.

    The response includes a ``players`` map with enriched details (web_name,
    team, position, price, code, expected_points) for every player in the
    report, so the dashboard can render photos, badges, and prices without
    additional lookups.
    """
    if not session_id:
        raise HTTPException(
            status_code=404,
            detail="No squad saved for this session",
        )
    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(
            status_code=404,
            detail="No squad saved for this session",
        )

    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"

    applied_overrides: list = []
    if live_facts:
        try:
            result = FactCollectionService().collect_overrides()
            applied_overrides = result.overrides
        except Exception as exc:  # noqa: BLE001 - fall back, never fail the request
            logger.warning(
                "Live fact collection failed; using baseline predictions. %s", exc
            )
            applied_overrides = []

    effective_provider = provider
    if applied_overrides:
        effective_provider = FactOverrideProvider(provider, applied_overrides)

    bridge = DecisionOptimizerBridge(provider=effective_provider)
    report = bridge.generate_decisions(squad)
    report.meta["live_facts_applied"] = len(applied_overrides)
    report.meta["player_positions"] = squad.player_positions or {}
    report.meta["live_fact_sources"] = sorted(
        {o.source.value for o in applied_overrides}
    )

    # --- chain provenance: which level actually served the numbers ------------
    # ``provider`` is the base quantitative provider (LivePredictionProvider in
    # production). FactOverrideProvider does not expose chain_meta, so we read
    # provenance from the *base* provider — the labels describe the underlying
    # quantitative signal, which is what the honest UI must disclose.
    chain_meta = _resolve_chain_meta(provider, report.gameweek)
    if chain_meta is not None:
        report.meta["chain"] = chain_meta

    # --- squad summary for the dashboard's summary bar -----------------------
    prices = squad.player_prices or {}
    total_value = round(sum(prices.values()) + float(squad.bank), 1)
    report.meta["squad_summary"] = {
        "team_value": total_value,
        "bank": round(float(squad.bank), 1),
        "free_transfers": squad.free_transfers,
        "chips_available": list(squad.chips_available or []),
    }

    # --- Understat xG/xA enrichment (matched players only) -------------------
    # The index is name-keyed; matching happens inside _build_player_details
    # where each player's web_name is already resolved.
    understat_index = _resolve_understat_index(provider)

    # Enrich with per-player details (names, teams, prices, codes, xPTS).
    report.players = _build_player_details(
        db, report, squad, effective_provider, understat_index=understat_index
    )
    return report


def _resolve_chain_meta(
    provider: DecisionPredictionProvider, gameweek: int
) -> dict[str, Any] | None:
    """Read provenance from a live provider; ``None`` for the static stub."""
    meta_getter = getattr(provider, "chain_meta", None)
    if not callable(meta_getter):
        return None
    try:
        return meta_getter(gameweek)
    except Exception as exc:  # noqa: BLE001 — provenance is best-effort
        logger.warning("chain_meta failed for gw%s: %s", gameweek, exc)
        return None


def _resolve_understat_index(
    provider: DecisionPredictionProvider,
) -> dict[str, dict[str, Any]] | None:
    """Return the live provider's name-keyed Understat index, if available.

    Only ``LivePredictionProvider`` exposes ``understat_index``. Returns an
    empty dict when enrichment is disabled/unavailable so callers can skip
    matching gracefully — never fabricating xG/xA for unmatched players.
    """
    index_getter = getattr(provider, "understat_index", None)
    if not callable(index_getter):
        return None
    try:
        return index_getter() or {}
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        logger.warning("understat_index failed: %s", exc)
        return {}


class FromFplRequest(BaseModel):
    """Request body for the one-click FPL team import."""

    entry_id: int = Field(
        ...,
        gt=0,
        description="Your FPL Manager Entry ID (the number in your FPL team URL).",
        examples=[1234567],
    )


@router.post("/squad/from-fpl", response_model=FromFplResponse, status_code=200)
async def import_squad_from_fpl(
    payload: FromFplRequest, db: GetDB, response: Response
) -> FromFplResponse:
    """One-click import: resolve an FPL Team ID into a saved squad."""
    importer = FplSquadImporter()
    try:
        result = await importer.build_squad_from_entry(payload.entry_id, db)
    except FplEntryNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Could not find FPL Team ID. Please check your number.",
        ) from exc
    except FplPicksNotSaved as exc:
        raise HTTPException(
            status_code=409,
            detail="Picks not saved yet",
        ) from exc
    except FplRateLimitBlocked as exc:
        logger.warning("FPL import failed (Rate limit): %s", exc)
        save_pending_sync(db, payload.entry_id)
        raise HTTPException(
            status_code=503,
            detail="FPL API blocked by rate limit",
        ) from exc
    except FplApiUnavailable as exc:
        logger.warning("FPL import failed (API unavailable): %s", exc)
        save_pending_sync(db, payload.entry_id)
        raise HTTPException(
            status_code=503,
            detail="FPL API is temporarily down, please try again in 5 minutes.",
        ) from exc

    saved = SquadService(session=db).set_squad(result.squad, session_id=str(payload.entry_id))
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    return FromFplResponse(
        squad=saved,
        player_names=result.player_names,
        entry_name=result.entry_name,
        gameweek=result.gameweek,
    )


@router.post("/squad/retry-sync", response_model=FromFplResponse, status_code=200)
async def retry_sync(request: Request, db: GetDB, response: Response) -> FromFplResponse:
    """Public, rate-limited retry for a queued auto-sync squad import.

    When :func:`import_squad_from_fpl` previously failed with a transient 503
    the manager's ``entry_id`` was queued with ``auto_sync=true``. This endpoint
    immediately retries that import (saving the squad and sending the "synced"
    Telegram push on success) — it is what the dashboard's ``🔄 Try Again``
    button calls.
    """
    settings = get_settings()
    host = request.client.host if request.client else "unknown"
    if _retry_sync_rate_limited(
        host,
        settings.retry_sync_rate_limit,
        settings.retry_sync_rate_window_seconds,
    ):
        raise HTTPException(
            status_code=429,
            detail="Too many retry requests. Please wait a minute and try again.",
        )

    try:
        # Capture the entry_id BEFORE the sync runs — on success the row is
        # marked SYNCED and no longer returned by get_pending_sync.
        pending_before = get_pending_sync(db)
        if pending_before is None:
            raise NoPendingSync("No pending sync found")
        session_key = str(pending_before.entry_id)
        result = await run_pending_sync(db)
    except NoPendingSync as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FplEntryNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Could not find FPL Team ID. Please check your number.",
        ) from exc
    except FplPicksNotSaved as exc:
        raise HTTPException(
            status_code=409,
            detail="Picks not saved yet",
        ) from exc
    except (FplRateLimitBlocked, FplApiUnavailable) as exc:
        logger.warning("Auto-sync retry failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="FPL API is temporarily down, please try again in a few minutes.",
        ) from exc

    saved = SquadService(session=db).set_squad(result.squad, session_id=session_key)
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    return FromFplResponse(
        squad=saved,
        player_names=result.player_names,
        entry_name=result.entry_name,
        gameweek=result.gameweek,
    )


@router.post("/squad/demo", response_model=FromFplResponse, status_code=200)
async def demo_squad(
    db: GetDB,
    response: Response,
    session_id: str | None = Query(
        None,
        description="Per-request session key. Generated by the caller so "
        "concurrent demo users get isolated squad rows.",
    ),
) -> FromFplResponse:
    """Build a valid demo squad from currently ingested players (no FPL account).

    Picks 2 GK, 5 DEF, 5 MID, 3 FWD from the players already in the database,
    assigns a sensible captain/vice, ~2.0 in the bank and 1 free transfer, then
    persists it via the SquadService so it renders exactly like a real squad.

    Each call generates a unique per-request ``session_id`` so concurrent demo
    users do not collide on a shared row.
    """
    from uuid import uuid4

    from fpl_intelligence.squad.demo import DemoSquadError

    try:
        squad = build_demo_squad(db)
    except DemoSquadError as exc:
        raise HTTPException(
            status_code=503,
            detail="Demo squad unavailable: not enough players ingested yet.",
        ) from exc

    player_names = {}
    for pid in squad.player_ids:
        p = db.get(Player, pid)
        player_names[pid] = p.web_name if p else f"Player {pid}"

    # Unique per-request key: concurrent "Try Demo Team" clicks get own rows.
    # The frontend may supply its own; otherwise we generate one server-side.
    demo_session_id = session_id or f"demo_{uuid4().hex}"
    saved = SquadService(session=db).set_squad(squad, session_id=demo_session_id)
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    return FromFplResponse(
        squad=saved,
        player_names=player_names,
        entry_name="Demo Squad",
        gameweek=squad.gameweek,
        is_demo=True,
    )
