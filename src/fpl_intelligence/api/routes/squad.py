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

import contextlib
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
from fpl_intelligence.db.models import Player
from fpl_intelligence.optimization.provider import DecisionPredictionProvider
from fpl_intelligence.prediction.live_provider import SOURCE_PROXY
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
    FplViewEntrySummary,
    FplViewHistory,
    FplViewPicks,
    FplViewResponse,
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
# v2.5.3 — per-session decisions cache keyed by snapshot updated_at
# --------------------------------------------------------------------------- #
_decisions_cache: dict[str, Any] = {}
_decisions_cache_lock = threading.Lock()


def _decisions_cache_key(session_id: str, updated_at: Any, gameweek: int) -> str:
    iso = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or "")
    return f"{session_id}:{iso}:{int(gameweek)}"


def _invalidate_decisions_cache(session_id: str) -> None:
    with _decisions_cache_lock:
        for k in list(_decisions_cache.keys()):
            if k.startswith(f"{session_id}:"):
                _decisions_cache.pop(k, None)

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
    _invalidate_decisions_cache(key)
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
    session_id: str | None = Query(None, description="Per-user session key. Required."),
    mode: str = Query(
        "plan",
        description="Phase 2 truth mode. 'plan' (default) returns the local "
        "Transfer-Planner overlay if present else the base FPL squad. 'fpl' "
        "returns the base FPL picks only (never the local overlay).",
        pattern="^(plan|fpl)$",
    ),
) -> SquadStateResponse:
    """Retrieve the *effective* squad state for a specific session.

    v2.7.3-dual-state: prefers the Transfer Planner's ``local_squad`` override;
    falls back to the last FPL-imported base row. ``session_id`` is REQUIRED.
    Phase 2: ``mode=fpl`` returns the FPL-truth base squad only (never the
    local overlay), so the UI can never show a planned player absent from FPL.
    Returns 404 if missing or if no squad has been saved for that key.
    """
    if not session_id:
        raise HTTPException(
            status_code=404,
            detail="No squad saved for this session",
        )
    squad = SquadService(session=db).get_effective_squad(session_id=session_id, mode=mode)
    if squad is None:
        raise HTTPException(
            status_code=404,
            detail="No squad saved for this session",
        )
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    return squad


@router.get(
    "/squad/base",
    response_model=SquadStateResponse,
    responses={404: {"description": "No squad saved for this session"}},
    include_in_schema=False,
)
async def get_base_squad(
    db: GetDB,
    response: Response,
    session_id: str | None = Query(None, description="Per-user session key. Required."),
) -> SquadStateResponse:
    """Raw FPL-truth squad (base) — diagnostic only, not used by UI."""
    if not session_id:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    response.headers["Cache-Control"] = "no-store"
    return squad


@router.get(
    "/squad/local",
    response_model=SquadStateResponse,
    responses={404: {"description": "No local squad saved"}},
    include_in_schema=False,
)
async def get_local_squad(
    db: GetDB,
    response: Response,
    session_id: str | None = Query(None, description="Per-user session key. Required."),
) -> SquadStateResponse:
    """Local Transfer Planner override — 404 when none saved."""
    if not session_id:
        raise HTTPException(status_code=404, detail="No local squad saved for this session")
    squad = SquadService(session=db).get_local_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(status_code=404, detail="No local squad saved for this session")
    response.headers["Cache-Control"] = "no-store"
    return squad


class LocalSaveBody(BaseModel):
    session_id: str = Field(..., description="Per-user session key (= FPL entry id)")
    element_in: int = Field(..., gt=0, description="Player id to bring in")
    element_out: int = Field(..., gt=0, description="Player id to take out")


@router.post(
    "/squad/local",
    response_model=SquadStateResponse,
    include_in_schema=False,
)
async def save_local_squad_swap(
    body: LocalSaveBody, db: GetDB, response: Response
) -> SquadStateResponse:
    """v2.7.3-dual-state: Save a staged IN/OUT to ``local_squad`` (no FPL fetch).

    Validates ``element_out in current effective squad`` and
    ``element_in not already owned``, computes a 15-man shadow list, copies
    captain/vice if they were moved, adjusts bank by price delta, enriches
    position/price/team from the catalog, persists via
    ``SquadService.set_local_squad`` and invalidates the decisions cache.
    """
    from fpl_intelligence.transfers.shadow import build_shadow_squad  # noqa: PLC0415

    svc = SquadService(session=db)
    cur = svc.get_effective_squad(session_id=body.session_id)
    if cur is None:
        raise HTTPException(status_code=404, detail="No squad saved for this session — import your team first.")
    shadow_ids = build_shadow_squad(list(cur.player_ids), int(body.element_out), int(body.element_in))
    if shadow_ids is None:
        raise HTTPException(status_code=422, detail="Staged transfer invalid: OUT not in squad or IN already owned.")

    # Resolve catalog for price/position/team enrichment.
    try:
        from fpl_intelligence.prediction.live_provider import load_player_catalog  # noqa: PLC0415

        catalog = load_player_catalog()
    except Exception:
        catalog = {}

    # Bank adjustment: bank + price_out - price_in (tenths via catalog now_cost)
    bank = float(cur.bank or 0.0)
    try:
        price_in = float(catalog.get(int(body.element_in), {}).get("price") or 0.0)
        price_out = float(catalog.get(int(body.element_out), {}).get("price") or 0.0)
        # Fallback to stored squad prices when catalog price missing.
        if not price_in:
            price_in = float((cur.player_prices or {}).get(int(body.element_in)) or 0.0)
        if not price_out:
            price_out = float((cur.player_prices or {}).get(int(body.element_out)) or 0.0)
        if price_in or price_out:
            bank = round(bank + price_out - price_in, 1)
    except Exception:
        pass

    # Captain/vice follow the swapped player.
    captain_id = int(cur.captain_id)
    vice_id = int(cur.vice_captain_id)
    if captain_id == int(body.element_out):
        captain_id = int(body.element_in)
    if vice_id == int(body.element_out):
        vice_id = int(body.element_in)

    # Enrich position/price/team for the new player.
    new_positions = dict(cur.player_positions or {})
    new_prices = dict(cur.player_prices or {})
    new_teams = dict(cur.player_teams or {})
    cat_row = catalog.get(int(body.element_in), {})
    if cat_row.get("position"):
        new_positions[int(body.element_in)] = int(cat_row["position"])
    new_positions.pop(int(body.element_out), None)
    if cat_row.get("price") is not None:
        new_prices[int(body.element_in)] = float(cat_row["price"])
    new_prices.pop(int(body.element_out), None)
    if cat_row.get("team") is not None:
        new_teams[int(body.element_in)] = int(cat_row["team"])
    new_teams.pop(int(body.element_out), None)

    payload = cur.model_copy(
        update={
            "player_ids": shadow_ids,
            "captain_id": captain_id,
            "vice_captain_id": vice_id,
            "bank": bank,
            "player_positions": new_positions or None,
            "player_prices": new_prices or None,
            "player_teams": new_teams or None,
            "session_id": body.session_id,
        }
    )
    # Strip updated_at so set_local_squad stamps fresh time.
    saved = svc.set_local_squad(
        SquadStateCreate(**{k: v for k, v in payload.model_dump().items() if k != "updated_at"}),
        session_id=body.session_id,
    )
    _invalidate_decisions_cache(body.session_id)
    response.headers["Cache-Control"] = "no-store"
    return saved


def _build_player_details(
    db: Session,
    report: DecisionReport,
    squad: SquadStateResponse,
    provider: DecisionPredictionProvider,
    understat_index: dict[str, dict[str, Any]] | None = None,
    ownership_map: dict[int, float] | None = None,
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
    omap = ownership_map if ownership_map is not None else {}

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
        predictions = provider.get_squad_predictions(list(player_ids), [report.gameweek])
    except Exception:  # noqa: BLE001 - xPTS is best-effort, never break the request
        predictions = {}
    gw_preds = predictions.get(report.gameweek, {})

    # Batch-resolve player rows once. The old implementation issued one
    # SELECT per player in the report, turning dashboard enrichment into an
    # N+1 database pattern on every uncached decisions request.
    players_by_element: dict[int, Player] = {}
    try:
        if player_ids:
            rows = db.scalars(
                select(Player).where(Player.fpl_element_id.in_(player_ids))
            ).all()
            players_by_element = {
                int(row.fpl_element_id): row
                for row in rows
                if row.fpl_element_id is not None
            }
    except Exception as exc:  # noqa: BLE001 - enrichment remains best-effort
        logger.debug("batched player enrichment query failed: %s", exc)

    details: dict[str, PlayerDetail] = {}
    for pid in sorted(player_ids):
        # R1: every stored player_id is a canonical FPL element id. Resolve the
        # Player row by that single key — name, team, position, price all come
        # from this same row, so a name can never be paired with another
        # player's price (which was the "Thiaw £15.5m" bug). Demo squads now
        # also store element ids, so there is exactly one code path here.
        player: Player | None = players_by_element.get(int(pid))
        if player is None:
            # Legacy fallback for rows seeded before the element-id migration:
            # the stored value is an internal auto-increment id.
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

        # xPTS breakdown: only when the resolved level is the proxy (it is the
        # only level that documents a formula). Baseline/backtest levels serve
        # from history/backtest and have no per-player breakdown to disclose.
        breakdown = None
        if pred is not None and getattr(pred, "source", None) == SOURCE_PROXY:
            raw_breakdown = pred.breakdown if hasattr(pred, "breakdown") else None
            if raw_breakdown and isinstance(raw_breakdown, dict):
                breakdown = {k: round(float(v), 2) for k, v in raw_breakdown.items()}

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
            xpts_breakdown=breakdown,
            ownership=omap.get(int(pid)),
        )

    return details


def _ownership_map(db: Session) -> dict[int, float]:
    """Element id -> selected-by percent (float), materialized facts first.

    Falls back to the committed bootstrap seed so ownership renders from the
    very first request after a fresh deploy (vaastav players_raw lags early
    in a season).
    """
    owners: dict[int, float] = {}
    try:
        from fpl_intelligence.sync.materialized_models import ElementFactDB

        for element_id, pct in db.execute(
            select(ElementFactDB.element_id, ElementFactDB.selected_by_percent)
        ).all():
            value = _parse_pct(pct)
            if value is not None:
                owners[int(element_id)] = value
    except Exception as exc:  # noqa: BLE001 — enrichment only
        db.rollback()  # a failed read must never leave the txn aborted
        logger.debug("element_facts ownership read failed: %s", exc)
    if len(owners) >= 100:
        return owners
    try:
        from fpl_intelligence.prediction.live_provider import load_player_catalog

        for element_id, row in load_player_catalog().items():
            if int(element_id) in owners:
                continue
            value = _parse_pct(row.get("selected_by_percent"))
            if value is not None:
                owners[int(element_id)] = value
    except Exception as exc:  # noqa: BLE001 — enrichment only
        logger.debug("seed catalog ownership fallback failed: %s", exc)
    return owners


def _parse_pct(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return round(float(raw), 1)
    except (TypeError, ValueError):
        return None


def _is_usable_squad(squad: SquadStateResponse | None) -> bool:
    """A squad is usable only when it actually carries players.

    v2.7.5-decisions-heal: the 03:20 UTC prod incident served
    ``{"generated_at": ...}`` because an empty ``local_squad_state`` row won the
    dual-state read and every optimizer silently produced an empty skeleton.
    An "effective" squad without player_ids is NOT effective — callers must
    fall back to the base row instead of optimizing nothing.
    """
    return squad is not None and bool(squad.player_ids)


async def build_decisions_payload(
    db: Session,
    provider: DecisionPredictionProvider,
    session_id: str,
    live_facts: bool = False,
) -> DecisionReport:
    """Run the FULL decisions chain for one session (v2.7.5-decisions-heal).

    Order of operations, each step guarded so the chain can never silently
    degrade to a bare ``generated_at`` skeleton:

    1. Effective squad (self-sealing local read → base fallback). When the
       effective row is missing or carries no players, the base FPL-truth row
       is used; when both are unusable the caller gets a 404 instead of an
       empty report.
    2. Target gameweek from the official clock (fallback: saved value).
    3. Optional live-fact overrides (never fatal).
    4. Full optimization chain via :class:`DecisionOptimizerBridge` — starting
       XI, bench order, captain, transfer plan, chip recommendation.
    5. Enrichment: chain provenance, squad summary, Understat xG/xA,
       ownership, per-player details, Phase 22 depth layers.
    6. Recommendation persistence (best-effort) and per-session caching.
    7. Skeleton guard: a populated squad MUST produce a non-empty starting XI;
       anything else raises loudly (surfaced as honest HTTP 503 by the route)
       rather than returning a hollow payload.
    """
    service = SquadService(session=db)

    # --- 1. squad resolution: effective → base → honest 404 ------------------
    squad = service.get_effective_squad(session_id=session_id)
    if not _is_usable_squad(squad):
        logger.warning(
            "decisions[%s]: effective squad empty/unusable (%s); falling back to base",
            session_id,
            "none" if squad is None else f"{len(squad.player_ids)} players",
        )
        squad = service.get_squad(session_id=session_id)
    if not _is_usable_squad(squad):
        raise HTTPException(
            status_code=404,
            detail="No squad saved for this session",
        )
    assert squad is not None  # narrowed above

    # --- 2. target gameweek follows the official FPL clock --------------------
    try:
        from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

        squad.gameweek = await resolve_target_gameweek(db, fallback=int(squad.gameweek))
    except Exception as exc:  # noqa: BLE001 - metadata only, never fail decisions
        logger.warning("target gameweek resolution failed: %s", exc)

    # --- v2.5.3 cache: keyed by updated_at so squad-pushes invalidate ----------
    if not live_facts:
        cache_key = _decisions_cache_key(session_id, squad.updated_at, squad.gameweek)
        with _decisions_cache_lock:
            cached = _decisions_cache.get(cache_key)
        if cached is not None:
            try:
                return cached.model_copy(deep=True)  # type: ignore[attr-defined]
            except Exception:
                return cached

    # --- 3/4. optional live facts + full optimization chain -------------------
    applied_overrides: list = []
    if live_facts:
        try:
            result = FactCollectionService().collect_overrides()
            applied_overrides = result.overrides
        except Exception as exc:  # noqa: BLE001 - fall back, never fail the request
            logger.warning("Live fact collection failed; using baseline predictions. %s", exc)
            applied_overrides = []

    effective_provider = provider
    if applied_overrides:
        effective_provider = FactOverrideProvider(provider, applied_overrides)

    bridge = DecisionOptimizerBridge(provider=effective_provider)
    report = bridge.generate_decisions(squad)

    # --- 7. skeleton guard -----------------------------------------------------
    if squad.player_ids and not report.starting_xi:
        raise RuntimeError(
            "decisions chain produced an empty starting XI for a populated "
            f"squad ({len(squad.player_ids)} players) — refusing to serve a "
            "hollow report"
        )

    report.meta["live_facts_applied"] = len(applied_overrides)
    report.meta["player_positions"] = squad.player_positions or {}
    report.meta["live_fact_sources"] = sorted({o.source.value for o in applied_overrides})

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
    ownership_map = _ownership_map(db)
    report.players = _build_player_details(
        db, report, squad, effective_provider, understat_index=understat_index,
        ownership_map=ownership_map,
    )

    # --- Phase 22 decision-depth layers ---------------------------------------
    try:
        await _attach_decision_depth(db, report, squad, ownership_map)
    except Exception as exc:  # noqa: BLE001 - depth layers are enhancements
        logger.warning("decision-depth enrichment failed: %s", exc)

    # --- Phase 2 math & model upgrades (ensemble CI, transfer EV, captain ---
    # confidence, differentials, price moves, injury risk). Purely additive
    # under ``report.meta['phase2']`` and never fatal.
    try:
        await _attach_phase2_insights(db, report, squad, ownership_map)
    except Exception as exc:  # noqa: BLE001 - Phase 2 insights are enhancements
        logger.warning("phase2 insights failed (non-fatal): %s", exc)

    # Phase 19.0 — persist this gameweek's calls so /track-record can grade
    # them once real results are ingested. Best-effort: tracking must never
    # break the decisions response.
    try:
        from fpl_intelligence.sync.service import record_recommendations

        record_recommendations(db, session_key=session_id, report=report)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - observability, not correctness
        logger.warning("recommendation recording failed for %s: %s", session_id, exc)
        db.rollback()

    # v2.5.3: cache the fully-enriched report keyed by updated_at so a fresh
    # squad-push is instantly visible and stale renders never survive a bump.
    if not live_facts:
        try:
            cache_key = _decisions_cache_key(session_id, squad.updated_at, squad.gameweek)
            with _decisions_cache_lock:
                _decisions_cache[cache_key] = report.model_copy(deep=True)  # type: ignore[attr-defined]
        except Exception:
            pass
    return report


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

    v2.7.5-decisions-heal: the entire chain lives in
    :func:`build_decisions_payload` (effective→base squad fallback, official
    clock, optimizer bridge, player enrichment, depth layers). This wrapper
    only owns transport concerns: ``no-store`` caching and converting an
    unexpected internal failure into an HONEST 503 payload — never a bare
    500 and never a skeleton report.
    """
    if not session_id:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    try:
        return await build_decisions_payload(db, provider, session_id, live_facts=live_facts)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - honest degradation, never a bare 500
        with contextlib.suppress(Exception):
            db.rollback()
        logger.exception("GET /decisions failed for %s; serving honest failure", session_id)
        raise HTTPException(
            status_code=503,
            detail=(
                "Decisions engine could not be computed right now "
                f"({type(exc).__name__}); retry shortly."
            ),
        ) from exc


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


# --------------------------------------------------------------------------- #
# Phase 22 — decision-depth layers (ownership / watchlist / captain cards)
# --------------------------------------------------------------------------- #


def _names_for(db: Session, pids: set[int]) -> dict[int, str]:
    """Resolve FPL element ids to display names (player table, seed fallback)."""
    names: dict[int, str] = {}
    for element_id, web_name in db.execute(
        select(Player.fpl_element_id, Player.web_name).where(
            Player.fpl_element_id.in_(pids or {0})
        )
    ).all():
        if element_id is not None and web_name:
            names[int(element_id)] = str(web_name)
    missing = pids - set(names)
    if missing:
        try:
            from fpl_intelligence.prediction.live_provider import load_player_catalog

            catalog = load_player_catalog()
            for pid in missing:
                row = catalog.get(int(pid))
                if row and row.get("web_name"):
                    names[int(pid)] = str(row["web_name"])
        except Exception as exc:  # noqa: BLE001 - display only
            logger.debug("seed name fallback failed: %s", exc)
    return names


def _prediction_rows(db: Session, gameweek: int) -> dict[int, float]:
    """All materialized xPTS rows for one gameweek -> ``{element_id: xpts}``."""
    from sqlalchemy import select

    from fpl_intelligence.sync.materialized_models import PredictionCurrentDB

    try:
        rows = db.execute(
            select(PredictionCurrentDB.element_id, PredictionCurrentDB.expected_points).where(
                PredictionCurrentDB.gameweek == int(gameweek)
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 — table may be absent on old deploys
        db.rollback()
        logger.debug("predictions_current read failed: %s", exc)
        return {}
    return {int(element_id): float(xpts) for element_id, xpts in rows if element_id is not None}


def _fdr_next3_by_team(
    rows_by_gw: dict[int, list[Any]], team_ids: set[int], horizon: list[int]
) -> dict[int, float | None]:
    """Average FDR over the next three unplayed fixtures per team id."""
    out: dict[int, float | None] = {tid: None for tid in team_ids}
    for tid in team_ids:
        diffs: list[float] = []
        for gw in horizon:
            for row in rows_by_gw.get(gw, ()):
                if row.finished:
                    continue
                if row.home_team == tid:
                    diffs.append(float(row.home_difficulty))
                    break
                if row.away_team == tid:
                    diffs.append(float(row.away_difficulty))
                    break
        if diffs:
            out[tid] = round(sum(diffs[:3]) / len(diffs[:3]), 2)
    return out


def _next_fixture_text(
    rows_by_gw: dict[int, list[Any]],
    horizon: list[int],
    team_names: Any,
    team_id: int | None,
) -> str | None:
    """Short "MCI(A)3" style label for a club's next unplayed fixture."""
    from fpl_intelligence.fixtures.scanner import team_short_name

    if team_id is None:
        return None
    for gw in horizon:
        for row in rows_by_gw.get(gw, ()):
            if row.finished:
                continue
            if row.home_team == team_id:
                return (
                    team_short_name(row.away_team, team_names)
                    + "(H)"
                    + str(row.home_difficulty)
                )
            if row.away_team == team_id:
                return (
                    team_short_name(row.home_team, team_names)
                    + "(A)"
                    + str(row.away_difficulty)
                )
    return None


def _team_ids_for(db: Session, pids: set[int]) -> dict[int, int]:
    teams: dict[int, int] = {}
    try:
        from fpl_intelligence.sync.materialized_models import ElementFactDB

        for element_id, team_id in db.execute(
            select(ElementFactDB.element_id, ElementFactDB.team_id).where(
                ElementFactDB.element_id.in_(pids or {0})
            )
        ).all():
            if element_id is not None and team_id is not None:
                teams[int(element_id)] = int(team_id)
    except Exception as exc:  # noqa: BLE001 — metadata only
        db.rollback()
        logger.debug("element_facts team read failed: %s", exc)
    return teams


def _seed_rows_for(pids: set[int]) -> dict[int, dict[str, Any]]:
    """Bootstrap-seed rows (position/team/price/web_name) for arbitrary ids."""
    try:
        from fpl_intelligence.prediction.live_provider import load_player_catalog

        catalog = load_player_catalog()
    except Exception as exc:  # noqa: BLE001 — metadata only
        logger.debug("seed catalog unavailable: %s", exc)
        return {}
    out: dict[int, dict[str, Any]] = {}
    for pid in pids:
        row = catalog.get(int(pid))
        if row:
            out[int(pid)] = row
    return out


async def _attach_decision_depth(
    db: Session,
    report: DecisionReport,
    squad: SquadStateResponse,
    ownership_map: dict[int, float],
) -> None:
    """Compute the D1/D2/D3 payloads onto ``report.meta`` (never raises upward).

    Everything reads materialized tables (predictions_current, fixtures_cache)
    plus the ownership map already resolved by the route — no live network.
    """
    from fpl_intelligence.api.routes.fixtures import load_fixtures
    from fpl_intelligence.fixtures.scanner import (
        average_fdr,
        next_gameweeks,
        parse_fixtures,
        player_run,
    )
    from fpl_intelligence.materialize import team_names_from_db
    from fpl_intelligence.squad.depth import (
        build_watchlist,
        captain_comparison,
        rank_differentials,
    )

    gameweek = int(report.gameweek)

    async def _run() -> None:
        xpts_all = _prediction_rows(db, gameweek)

        fixtures_raw: list[dict[str, Any]] = []
        try:
            fixtures_raw = await load_fixtures(db)
        except Exception as exc:  # noqa: BLE001 — FDR context is optional
            logger.warning("fixtures unavailable for depth layers: %s", exc)
        fixture_rows = parse_fixtures(fixtures_raw)
        rows_by_gw: dict[int, list[Any]] = {}
        for row in fixture_rows:
            rows_by_gw.setdefault(row.event, []).append(row)
        horizon5 = next_gameweeks(fixture_rows, gameweek, 5)
        team_names = team_names_from_db(db) or {}

        squad_ids = {int(p) for p in squad.player_ids}

        # --- D1 differential strip -------------------------------------------
        differentials = rank_differentials(
            xpts_all, ownership_map, exclude_ids=squad_ids
        )
        diff_names = _names_for(db, {d["player_id"] for d in differentials})
        diff_seed = _seed_rows_for({d["player_id"] for d in differentials})
        diff_teams = _team_ids_for(db, {d["player_id"] for d in differentials})
        for d in differentials:
            pid = d["player_id"]
            detail = report.players.get(str(pid))
            seed = diff_seed.get(pid)
            d["web_name"] = (
                (detail.web_name if detail else None)
                or diff_names.get(pid)
                or f"Player {pid}"
            )
            position = detail.position if detail else None
            if position is None and seed is not None:
                position = seed.get("position")
            d["position"] = position
            price = detail.price if detail else None
            if price is None and seed is not None and seed.get("now_cost") is not None:
                price = round(float(seed["now_cost"]) / 10.0, 1)
            d["price"] = price
            d["next_fixture"] = _next_fixture_text(
                rows_by_gw, horizon5, team_names, diff_teams.get(pid)
            )
        report.meta["differential_picks"] = differentials

        # --- D2 transfer watchlist --------------------------------------------
        outs = list((report.transfer_plan.transfers_out if report.transfer_plan else []) or [])
        needed_positions: list[int] = []
        for pid in outs:
            detail = report.players.get(str(pid))
            if detail and detail.position:
                needed_positions.append(int(detail.position))
        if not needed_positions:
            # Roll verdict: surface the two weakest starter positions by xPTS.
            xi_xpts: dict[int, float] = {}
            for pid in report.starting_xi:
                detail = report.players.get(str(pid))
                pos = int(detail.position) if detail and detail.position else None
                if pos is None:
                    continue
                xi_xpts[pos] = xi_xpts.get(pos, 0.0) + float(detail.expected_points or 0.0)
            outfield = [1, 2, 3, 4]
            needed_positions = [
                pos
                for pos in sorted(outfield, key=lambda p: xi_xpts.get(p, 0.0))[:2]
            ]
        needed_positions = sorted(set(needed_positions))

        candidates: list[dict[str, Any]] = []
        if needed_positions:
            candidate_ids = {
                pid for pid, xpts in xpts_all.items() if pid not in squad_ids
            }
            seed_rows = _seed_rows_for(candidate_ids)
            cand_teams = _team_ids_for(db, candidate_ids)
            # Seed catalog fills team ids the facts table has not mirrored yet.
            for pid, row in seed_rows.items():
                if pid not in cand_teams and row.get("team") is not None:
                    cand_teams[pid] = int(row["team"])
            cand_names = _names_for(db, candidate_ids)
            for pid in candidate_ids:
                detail = report.players.get(str(pid))
                seed = seed_rows.get(pid)
                position = detail.position if detail else None
                if position is None and seed is not None:
                    position = seed.get("position")
                if position is None or int(position) not in needed_positions:
                    continue
                price = detail.price if detail else None
                if price is None and seed is not None and seed.get("now_cost") is not None:
                    price = round(float(seed["now_cost"]) / 10.0, 1)
                runs = player_run(cand_teams.get(pid), rows_by_gw, horizon5, team_names=team_names)
                real_runs = [r for r in runs if r.opponent_id != 0]
                candidates.append(
                    {
                        "player_id": pid,
                        "web_name": (detail.web_name if detail else None)
                        or cand_names.get(pid)
                        or f"Player {pid}",
                        "position": int(position),
                        "price": price,
                        "xpts": xpts_all[pid],
                        "fdr_next3": round(average_fdr(real_runs[:3]), 2)
                        if len(real_runs) >= 1
                        else None,
                        "ownership_pct": ownership_map.get(pid),
                    }
                )
        watchlist = build_watchlist(candidates, needed_positions=needed_positions)
        watchlist["verdict"] = (
            report.transfer_plan.action_type if report.transfer_plan else "roll"
        )
        report.meta["transfer_watchlist"] = watchlist

        # --- D3 captain comparison + vice EV line ------------------------------
        xi_ids = {int(pid) for pid in report.starting_xi}
        xi_teams = _team_ids_for(db, xi_ids)
        for pid, row in _seed_rows_for(xi_ids).items():
            if pid not in xi_teams and row.get("team") is not None:
                xi_teams[pid] = int(row["team"])
        xi_data: list[dict[str, Any]] = []
        for pid in report.starting_xi:
            detail = report.players.get(str(pid))
            if detail is None:
                continue
            xi_data.append(
                {
                    "player_id": int(pid),
                    "web_name": detail.web_name,
                    "xpts": detail.expected_points,
                    "ownership_pct": detail.ownership,
                    "next_fixture": _next_fixture_text(
                        rows_by_gw, horizon5, team_names, xi_teams.get(int(pid))
                    ),
                }
            )
        comparison = captain_comparison(
            xi_data,
            report.captain.player_id if report.captain else None,
            report.vice_captain,
        )
        # Phase 24 C2 — highlight set-piece takers in captain comparison
        try:
            from fpl_intelligence.set_pieces.service import (
                set_piece_flags as _sp_flags,  # noqa: PLC0415
            )

            for card in comparison.get("cards", []) or []:
                pid = int(card.get("player_id", 0) or 0)
                team = xi_teams.get(pid)
                card["set_pieces"] = _sp_flags(pid, team)
        except Exception:
            pass
        report.meta["captain_comparison"] = comparison

    await _run()


class FromFplRequest(BaseModel):
    """Request body for the one-click FPL team import."""

    entry_id: int = Field(
        ...,
        gt=0,
        description="Your FPL Manager Entry ID (the number in your FPL team URL).",
        examples=[1234567],
    )
    session_id: str | None = Field(
        default=None,
        description="Optional session key to persist under. When omitted, "
        "defaults to str(entry_id). Phase 1: the squad is always stored under "
        "this key so a refresh/restore keeps the same session.",
        examples=["1234567"],
    )


# --------------------------------------------------------------------------- #
# Phase 2 — math & model upgrades (meta['phase2'] enrichment).
# --------------------------------------------------------------------------- #


async def _attach_phase2_insights(
    db: Session,
    report: DecisionReport,
    squad: SquadStateResponse,
    ownership_map: dict[int, float],
) -> None:
    """Compute Phase 2 insights onto ``report.meta['phase2']`` (never fatal).

    Everything reads materialized tables plus the committed seed catalog —
    no live network. Each sub-analysis stays independent so a missing table
    degrades its own section only, per the Phase 2 Safety rule.
    """
    from sqlalchemy import select

    from fpl_intelligence.db.models import Player, PlayerGameweekPerformance
    from fpl_intelligence.models.captaincy import captain_confidence_detail
    from fpl_intelligence.models.ensemble_xpts import (
        calculate_ensemble_xpts,
        collect_player_inputs_batch,
    )
    from fpl_intelligence.prediction.live_provider import load_player_catalog
    from fpl_intelligence.sync.materialized_models import (
        ElementFactDB,
        PredictionCurrentDB,
    )
    from fpl_intelligence.tools.differentials import find_differentials
    from fpl_intelligence.tools.injury_risk import calculate_injury_risk
    from fpl_intelligence.tools.price_predictor import predict_price_changes
    from fpl_intelligence.tools.transfer_ev import get_top_transfers

    gameweek = int(report.gameweek)
    horizon = list(range(gameweek, gameweek + 5))
    xpts_all = _prediction_rows(db, gameweek)

    # --- multi-GW horizon predictions {element: {gw: {"mean": m}}} -----------
    horizon_preds: dict[int, dict[int, dict[str, float]]] = {}
    for element_id, gw, pts in db.execute(
        select(
            PredictionCurrentDB.element_id,
            PredictionCurrentDB.gameweek,
            PredictionCurrentDB.expected_points,
        ).where(PredictionCurrentDB.gameweek.in_(horizon))
    ).all():
        horizon_preds.setdefault(int(element_id), {})[int(gw)] = {
            "mean": float(pts)
        }

    phase2: dict[str, Any] = {"model": "ensemble_v1", "gameweek": gameweek}
    squad_ids = {int(p) for p in squad.player_ids}

    # --- market rows for the tools (£m prices converted from tenth-units) ----
    catalog = load_player_catalog()
    latest_transfers: dict[int, tuple[int | None, int | None]] = {}
    for el, tin, tout in db.execute(
        select(
            Player.fpl_element_id,
            PlayerGameweekPerformance.transfers_in,
            PlayerGameweekPerformance.transfers_out,
        )
        .join(
            PlayerGameweekPerformance,
            PlayerGameweekPerformance.player_id == Player.id,
        )
        .where(Player.fpl_element_id.isnot(None))
        .order_by(PlayerGameweekPerformance.gameweek_id.desc())
        .limit(5000)
    ).all():
        if int(el) not in latest_transfers:
            latest_transfers[int(el)] = (tin, tout)

    def _market_row(el: int) -> dict[str, Any]:
        row = dict(catalog.get(int(el), {}) or {})
        tin, tout = latest_transfers.get(int(el), (None, None))
        row.update({"id": int(el), "transfers_in": tin, "transfers_out": tout})
        # Unit normalisation: tools speak £m floats, the catalog speaks
        # tenth-units. A sane £m price never exceeds 20, anything bigger is
        # unmistakably tenths.
        raw_cost = row.get("now_cost", row.get("price"))
        try:
            cost = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            cost = None
        if cost is not None:
            row["now_cost"] = round(cost / 10.0, 2) if cost > 20 else round(cost, 2)
        return row

    all_market = [_market_row(el) for el in sorted(set(catalog) | set(latest_transfers))]

    # --- 2.4 differentials ----------------------------------------------------
    current_preds = {el: {gameweek: {"mean": v}} for el, v in xpts_all.items()}
    phase2["differentials"] = find_differentials(
        all_market, current_preds, gameweek, exclude_ids=squad_ids
    )

    # --- 2.5 price-change predictions ----------------------------------------
    phase2["price_changes"] = predict_price_changes(all_market, limit=15)

    # --- 2.2 transfer EV (bank-limited search over materialized horizon) -----
    try:
        bank_millions = float(getattr(squad, "bank", 0.0) or 0.0)
    except (TypeError, ValueError):
        bank_millions = 0.0
    phase2["transfer_ev"] = {
        "horizon_gws": horizon,
        "bank": round(bank_millions, 1),
        "top": get_top_transfers(squad, bank_millions, all_market, horizon_preds),
    }

    # --- element -> internal-id map + fact table for the watched players -----
    from types import SimpleNamespace

    watch_ids = (
        set(report.starting_xi)
        | ({int(report.captain.player_id)} if report.captain else set())
        or set(squad_ids)
    )
    id_map: dict[int, int] = {}
    minutes_by_el: dict[int, float] = {}
    if watch_ids:
        for el, pid in db.execute(
            select(Player.fpl_element_id, Player.id).where(
                Player.fpl_element_id.in_(watch_ids)
            )
        ).all():
            if el is not None:
                id_map[int(el)] = int(pid)
        for el, minutes in db.execute(
            select(ElementFactDB.element_id, ElementFactDB.minutes).where(
                ElementFactDB.element_id.in_(watch_ids)
            )
        ).all():
            minutes_by_el[int(el)] = float(minutes or 0.0)

    # --- 2.1 ensemble xPTS (with confidence interval) for the starting XI ----
    # Batch watched-player histories into one SQL statement.
    internal_watch_ids = sorted({internal for internal in id_map.values()})
    history_by_internal = (
        collect_player_inputs_batch(db, internal_watch_ids)
        if internal_watch_ids
        else {}
    )
    ensemble: dict[str, Any] = {}
    for el in sorted(watch_ids):
        internal = id_map.get(el)
        data = dict(history_by_internal.get(internal, {})) if internal else {}
        data["baseline_xpts"] = xpts_all.get(el)
        # FDR per element is not materialized yet → factor drops out honestly
        # and weights renormalise inside the model.
        stub = SimpleNamespace(fixture_difficulty=None)
        ensemble[str(el)] = calculate_ensemble_xpts(stub, gameweek, data)

    # --- 2.3 captain confidence (dynamic replacement for the static 70%) ----
    captain_id = int(report.captain.player_id) if report.captain else None
    ranked = sorted(
        ((int(el), float(xpts_all.get(el, 0.0))) for el in report.starting_xi),
        key=lambda t: (-t[1], t[0]),
    )
    top_el = (
        captain_id
        if captain_id is not None and any(e == captain_id for e, _ in ranked)
        else (ranked[0][0] if ranked else None)
    )
    second_xpts = next((v for e, v in ranked if e != top_el), None)
    detail: dict[str, Any] = {
        "score": 50.0,
        "complete": False,
        "note": "no starting XI predictions available",
    }
    if top_el is not None and top_el in xpts_all:
        internal = id_map.get(top_el)
        hist = history_by_internal.get(internal, {}).get("points_history", []) if internal else []
        recent5 = hist[-5:]
        detail = captain_confidence_detail(
            SimpleNamespace(
                xpts=xpts_all.get(top_el),
                fixture_difficulty=None,  # not materialized yet — drops out
                form_avg=sum(recent5) / len(recent5) if recent5 else None,
                season_avg=sum(hist) / len(hist) if hist else None,
                selected_by_percent=ownership_map.get(top_el),
            ),
            SimpleNamespace(xpts=second_xpts),
            gameweek,
        )
    detail["top_pick"] = top_el
    phase2["captain_confidence"] = detail

    # --- 2.6 injury / suspension risk over the same watchlist -----------------
    phase2["injury_risk"] = [
        calculate_injury_risk(
            SimpleNamespace(
                id=el,
                web_name=(catalog.get(el, {}) or {}).get("web_name"),
                minutes_played=minutes_by_el.get(el),
            )
        )
        for el in sorted(watch_ids)
    ]

    phase2["ensemble_xpts"] = ensemble
    report.meta["phase2"] = phase2


def _build_transfer_status(result: Any) -> str | None:
    """Honest transfer verdict — never derived from an internal snapshot diff.

    A confirmed transfer is reported ONLY when one of the official FPL signals
    is present:
    * the squad was rebuilt from FPL's official ``/history/`` element_in/out
      (``rebuilt_from_history``), or
    * FPL's history recorded zero transfers for the target GW (``no_pending_transfer``
      → "Matches FPL picks — no confirmed transfer.").

    In every other case we report the neutral line rather than guessing from a
    snapshot diff, so the UI never shows "Cash in / De Cuyper out" as fact.
    """
    if getattr(result, "rebuilt_from_history", False):
        gw = getattr(result, "pending_transfer_gw", None) or "next"
        return (
            f"Confirmed transfer applied for GW{gw} (from official FPL history). "
            "Your FPL squad now reflects this move."
        )
    if getattr(result, "no_pending_transfer", False):
        return "Matches FPL picks — no confirmed transfer."
    return "Squad imported from FPL — no confirmed transfer to report."


def _build_sync_status(result: Any, entry_id: int) -> str:
    """Build an honest sync-status line naming the egress mask that won."""
    strategy = getattr(result, "winning_strategy", None)
    base = f"Synced via {strategy} — FPL ID {entry_id} saved." if strategy else (
        f"FPL ID {entry_id} saved."
    )
    # v2.6.0 — surface the transfer-saga truth branches on the status line.
    if getattr(result, "rebuilt_from_history", False):
        gw = getattr(result, "pending_transfer_gw", None) or "next"
        return f"GW{gw} squad rebuilt from official FPL history. {base}"
    if getattr(result, "no_pending_transfer", False):
        gw = getattr(result, "pending_transfer_gw", None) or "next"
        return (
            f"No confirmed transfer found on FPL for GW{gw} — "
            f"finish it on FPL, then sync. {base}"
        )
    return base


@router.post("/squad/from-fpl", response_model=FromFplResponse, status_code=200)
async def import_squad_from_fpl(
    payload: FromFplRequest, db: GetDB, response: Response
) -> FromFplResponse:
    """One-click import: resolve an FPL Team ID into a saved squad.

    FPL API traffic is routed through the egress chain (direct → allorigins →
    corsproxy.io → ``$FPL_PROXY_URL``) so a blocked path falls through instead
    of 500-ing. Any failure returns HTTP 503 with a sync-payload and a truthful
    status line (winning mask or "blocked", next retry time) — never a bare 500.
    On success the sync-status line names the mask that reached FPL.
    """
    from fpl_intelligence.data_providers.registry import get_async_fpl_adapter

    egress = get_async_fpl_adapter(settings=get_settings())
    importer = FplSquadImporter(egress=egress)
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
    except Exception as exc:  # noqa: BLE001 - catch-all: never surface a bare 500
        logger.exception("FPL import failed (unexpected): %s", exc)
        try:
            save_pending_sync(db, payload.entry_id)
        except Exception as sync_exc:  # noqa: BLE001
            logger.warning("save_pending_sync failed: %s", sync_exc)
        raise HTTPException(
            status_code=503,
            detail="FPL import failed (external): contact support if this persists. "
            "Your ID is saved — we retry automatically and will Telegram you on success.",
        ) from exc

    # Phase 1: the squad is always persisted under the entry_id unless an
    # explicit session_id was supplied in the body (e.g. a reconnect of the
    # same manager). Refresh/restore then keeps one stable session key.
    effective_session = payload.session_id or str(payload.entry_id)

    saved = SquadService(session=db).set_squad(result.squad, session_id=effective_session)
    _invalidate_decisions_cache(effective_session)

    # Phase 2 — after "Sync from FPL" the user is in FPL mode: the base (FPL
    # truth) row is authoritative. Drop any stale local Transfer-Planner
    # override so the effective squad can never bleed in a planned player
    # (e.g. De Cuyper) that is absent from the official picks. The planner is
    # an overlay only; it is re-applied by the user if they opt to keep it.
    try:
        SquadService(session=db).clear_local(effective_session)
    except Exception as exc:  # noqa: BLE001 — overlay clear is best-effort
        logger.debug("clear_local on sync failed (ignored): %s", exc)

    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    sync_status = _build_sync_status(result, entry_id=payload.entry_id)

    # Phase 2 — honest transfer verdict. A confirmed transfer is reported ONLY
    # when FPL's official history recorded one (rebuilt_from_history) or the
    # import found no pending transfer (no_pending_transfer). The internal
    # snapshot-diff (detected_transfer) is NEVER surfaced as confirmed — we
    # demote it to None so the UI cannot present a guessed move as fact.
    transfer_status = _build_transfer_status(result)

    return FromFplResponse(
        squad=saved,
        player_names=result.player_names,
        entry_name=result.entry_name,
        gameweek=result.gameweek,
        sync_status=sync_status,
        detected_transfer=None,
        transfer_status=transfer_status,
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
    except Exception as exc:  # noqa: BLE001 - never surface a bare 500 on retry
        logger.exception("Auto-sync retry failed (unexpected): %s", exc)
        raise HTTPException(
            status_code=503,
            detail="FPL API is temporarily down, please try again in a few minutes.",
        ) from exc

    saved = SquadService(session=db).set_squad(result.squad, session_id=session_key)
    _invalidate_decisions_cache(session_key)
    # Phase 2 — re-sync returns the user to FPL mode; drop any stale local
    # Transfer-Planner overlay so the effective squad matches FPL truth.
    try:
        SquadService(session=db).clear_local(session_key)
    except Exception as exc:  # noqa: BLE001 — overlay clear is best-effort
        logger.debug("clear_local on retry-sync failed (ignored): %s", exc)
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    sync_status = _build_sync_status(result, entry_id=pending_before.entry_id)
    transfer_status = _build_transfer_status(result)
    return FromFplResponse(
        squad=saved,
        player_names=result.player_names,
        entry_name=result.entry_name,
        gameweek=result.gameweek,
        sync_status=sync_status,
        detected_transfer=None,
        transfer_status=transfer_status,
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
    _invalidate_decisions_cache(demo_session_id)
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    return FromFplResponse(
        squad=saved,
        player_names=player_names,
        entry_name="Demo Squad",
        gameweek=squad.gameweek,
        is_demo=True,
    )


@router.get("/squad/sync-status")
async def sync_status(
    response: Response,
    session_id: str = Query(..., description="Per-user session key (= FPL entry id)."),
) -> dict[str, Any]:
    """Poll the async sync-now job for a session.

    Returns ``{state: running|done|failed, banner, picks_gw, ...}``.
    """
    if not session_id or not str(session_id).strip():
        raise HTTPException(status_code=400, detail="session_id is required")
    from fpl_intelligence.squad.sync_job import get_job  # noqa: PLC0415

    job = get_job(str(session_id))
    if job is None:
        raise HTTPException(status_code=404, detail="No sync job for this session")
    response.headers["Cache-Control"] = "no-store"
    return {
        "state": job["state"],
        "job_id": job["job_id"],
        "session_id": job["session_id"],
        "banner": job.get("banner"),
        "picks_gw": job.get("picks_gw"),
        "gameweek": job.get("gameweek"),
        "transfers_in": job.get("transfers_in", []),
        "transfers_out": job.get("transfers_out", []),
        "before_ids": job.get("before_ids", []),
        "after_ids": job.get("after_ids", []),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "next_gw": job.get("next_gw"),
        "detected_transfer": job.get("detected_transfer"),
        # v2.5.7 honesty fields
        "chose_rule": job.get("chose_rule"),
        "picks_next_status": job.get("picks_next_status"),
        "ids_hash_current": job.get("ids_hash_current"),
        "ids_hash_next": job.get("ids_hash_next"),
    }


@router.get("/squad/fpl-view", response_model=FplViewResponse)
async def fpl_view(
    response: Response,
    session_id: str = Query(..., description="Per-user session key (= FPL entry id)."),
) -> FplViewResponse:
    """Raw FPL truth via egress masks — no mutation, no guessing.

    Returns exactly what FPL shows for this entry:
    - current_event: the FPL current gameweek
    - picks_current: {gw, ids, status} for current_event
    - picks_next: {gw, ids, status} for next unplayed GW (status 200/404)
    - entry_summary: last-deadline bank/transfers from entry endpoint
    - fpl_history (v2.6.0): official /history/ row cross-check for the target
      GW ("FPL history: N transfer(s) made for GWX").
    """
    if not session_id or not str(session_id).strip().isdigit():
        raise HTTPException(status_code=400, detail="session_id must be a numeric entry id")

    from fpl_intelligence.config import get_settings
    from fpl_intelligence.data_providers.registry import get_async_fpl_adapter
    from fpl_intelligence.squad.fpl_import import FplSquadImporter
    from fpl_intelligence.squad.fpl_truth import fetch_fpl_truth, history_note

    egress = get_async_fpl_adapter(settings=get_settings())
    importer = FplSquadImporter(egress=egress)

    from fpl_intelligence.squad.fpl_import import FplApiUnavailable

    try:
        truth = await fetch_fpl_truth(int(session_id), importer)
    except FplApiUnavailable as exc:
        # Audit 2026-08: a fully-exhausted egress chain (FPL blocks the host,
        # every mask down, FPL_PROXY_URL unset) used to escape as a bare 500.
        # Degrade honestly instead — this is exactly the shared-egress 403/429
        # scenario the mask chain exists for.
        raise HTTPException(
            status_code=503,
            detail=(
                "FPL is unreachable from this deployment right now "
                "(all egress strategies failed); retry shortly."
            ),
        ) from exc

    current_event = truth.current_event
    next_gw = truth.next_gw or current_event

    entry_name = truth.entry_name
    # Entry summary needs raw entry fields (bank/transfers) — re-read cheaply
    # through the same egress chain's 60s cache when available.
    try:
        raw_entry = await egress.fetch(
            f"/api/entry/{session_id}/",
            validator=lambda d: (
                None
                if isinstance(d, dict) and "id" in d
                else (_ for _ in ()).throw(ValueError("entry payload missing 'id'"))
            ),
        )
    except Exception:
        raw_entry = {}

    response.headers["Cache-Control"] = "no-store"
    return FplViewResponse(
        current_event=current_event,
        picks_current=FplViewPicks(
            gw=current_event,
            ids=truth.picks_current_ids,
            status=truth.picks_current_status,
        ),
        picks_next=FplViewPicks(
            gw=next_gw, ids=truth.picks_next_ids, status=truth.picks_next_status
        ),
        entry_summary=FplViewEntrySummary(
            name=entry_name,
            id=int((raw_entry or {}).get("id", session_id)),
            current_event=current_event,
            last_deadline_bank=(raw_entry or {}).get("last_deadline_bank"),
            last_deadline_total_transfers=(raw_entry or {}).get("last_deadline_total_transfers"),
            last_deadline_bank_tenths=(raw_entry or {}).get("last_deadline_bank"),
        ),
        fpl_history=FplViewHistory(
            gw=next_gw,
            event_transfers=truth.history_event_transfers(),
            event_transfers_cost=(truth.history_row or {}).get("event_transfers_cost")
            if truth.history_row
            else None,
            latest_event=int(truth.latest_history_row.get("event") or 0)
            if truth.latest_history_row
            else None,
            latest_event_transfers=int(truth.latest_history_row.get("event_transfers") or 0)
            if truth.latest_history_row
            else None,
            note=history_note(truth),
        ),
    )


@router.post("/squad/sync-now", status_code=200)
async def sync_now(
    db: GetDB,
    response: Response,
    session_id: str = Query(..., description="Per-user session key (= FPL entry id)."),
    next_gw: bool = Query(
        False,
        description="When true, force picks_gw to the next unplayed GW (pre-deadline toggle).",
    ),
) -> dict[str, Any]:
    """Dashboard 'Sync now' — async job pattern (v2.5.6).

    Starts a background task (25s internal cap, parallel fetch via asyncio.gather,
    bootstrap cached 10m, picks cached 60s) keyed by session. Returns 202
    ``{job_id, state:"running"}`` immediately. If the job finishes <4s,
    returns ``state:"done"`` directly with the banner. Poll
    ``GET /squad/sync-status?session_id=`` every 2s (max 30s) for the result.
    """

    if not session_id or not str(session_id).strip().isdigit():
        raise HTTPException(status_code=400, detail="session_id must be a numeric entry id")

    # Resolve engine bind for background task (so it shares the same DB in tests)
    try:
        engine_bind = db.get_bind()  # type: ignore[attr-defined]
        if engine_bind is None:
            engine_bind = getattr(db, "bind", None)
    except Exception:
        engine_bind = getattr(db, "bind", None)
    if engine_bind is None:
        try:
            from fpl_intelligence.db.session import engine as _engine  # noqa: PLC0415

            engine_bind = _engine
        except Exception:
            engine_bind = None
    if engine_bind is None:
        raise HTTPException(status_code=500, detail="Database engine unavailable")

    from fpl_intelligence.squad.sync_job import (  # noqa: PLC0415
        get_job,
        start_sync_job,
        wait_for_job_fast_poll,
    )

    job, _handle = start_sync_job(str(session_id), bool(next_gw), engine_bind)

    # Fast path: if the whole job (fetch + save) resolves <4s, return done directly.
    # v2.6.0 serverless fix: on Vercel the background THREAD dies the moment we
    # return 202 (instance freeze), so while running there we keep the request
    # alive and poll inline up to ~45s (maxDuration is 60s). Locally the legacy
    # 4s fast path + 202/poll behaviour is preserved.
    import os as _os

    on_vercel = _os.getenv("VERCEL", "") == "1"
    completed = await wait_for_job_fast_poll(str(session_id), timeout=4.0)
    if not completed and on_vercel:
        completed = await wait_for_job_fast_poll(str(session_id), timeout=41.0)
        job_after_running = get_job(str(session_id)) or {}
        if (
            not completed
            and job_after_running.get("state") == "failed"
        ):
            completed = True
    if completed:
        job_after = get_job(str(session_id))
        if job_after is None:
            job_after = job
        state = job_after.get("state")
        if state == "done":
            response.headers["Cache-Control"] = "no-store"
            return {
                "job_id": job_after["job_id"],
                "state": "done",
                "ok": True,
                "session_id": str(session_id),
                "gameweek": job_after.get("gameweek"),
                "picks_gw": job_after.get("picks_gw"),
                "banner": job_after.get("banner"),
                "before_ids": job_after.get("before_ids", []),
                "after_ids": job_after.get("after_ids", []),
                "transfers_in": job_after.get("transfers_in", []),
                "transfers_out": job_after.get("transfers_out", []),
                "detected_transfer": job_after.get("detected_transfer"),
                "started_at": job_after.get("started_at"),
                "finished_at": job_after.get("finished_at"),
                "synced_at": job_after.get("finished_at"),
                # v2.5.7 honesty fields
                "chose_rule": job_after.get("chose_rule"),
                "picks_next_status": job_after.get("picks_next_status"),
                "ids_hash_current": job_after.get("ids_hash_current"),
                "ids_hash_next": job_after.get("ids_hash_next"),
            }
        if state == "failed":
            response.headers["Cache-Control"] = "no-store"
            # Return 200 with failed state so UI can show honest reason + Retry without polling
            return {
                "job_id": job_after["job_id"],
                "state": "failed",
                "ok": False,
                "session_id": str(session_id),
                "error": job_after.get("error"),
                "banner": job_after.get("banner"),
                "started_at": job_after.get("started_at"),
                "finished_at": job_after.get("finished_at"),
            }
        # Still running but wait returned? Fall through to 202
    # Still running after 4s
    response.status_code = 202
    response.headers["Cache-Control"] = "no-store"
    return {
        "job_id": job["job_id"],
        "state": "running",
        "session_id": str(session_id),
        "started_at": job["started_at"],
        "next_gw": bool(next_gw),
    }
