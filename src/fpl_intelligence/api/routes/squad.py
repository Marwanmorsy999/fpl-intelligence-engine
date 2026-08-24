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
from fpl_intelligence.data_providers.fpl_egress import FplEgressChain
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
    session_id: str | None = Query(None, description="Per-user session key. Required."),
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

    details: dict[str, PlayerDetail] = {}
    for pid in sorted(player_ids):
        # R1: every stored player_id is a canonical FPL element id. Resolve the
        # Player row by that single key — name, team, position, price all come
        # from this same row, so a name can never be paired with another
        # player's price (which was the "Thiaw £15.5m" bug). Demo squads now
        # also store element ids, so there is exactly one code path here.
        player: Player | None = db.scalar(select(Player).where(Player.fpl_element_id == pid))
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
        raise HTTPException(status_code=404, detail="No squad saved for this session")

    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"

    # Phase 21.1 (T2): the target gameweek follows the official FPL clock at
    # request time — bootstrap next-deadline event, fixtures-cache fallback,
    # then the saved squad value. The header and every downstream number use
    # this one value.
    try:
        from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

        squad.gameweek = await resolve_target_gameweek(db, fallback=int(squad.gameweek))
    except Exception as exc:  # noqa: BLE001 - metadata only, never fail decisions
        logger.warning("target gameweek resolution failed: %s", exc)

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


def _build_sync_status(result: Any, entry_id: int) -> str:
    """Build an honest sync-status line naming the egress mask that won."""
    strategy = getattr(result, "winning_strategy", None)
    if strategy:
        return f"Synced via {strategy} — FPL ID {entry_id} saved."
    return f"FPL ID {entry_id} saved."


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
    settings = get_settings()
    egress = FplEgressChain(
        settings.fpl_base_url,
        timeout=settings.egress_strategy_timeout,
        cache_ttl=settings.egress_cache_ttl,
    )
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

    saved = SquadService(session=db).set_squad(result.squad, session_id=str(payload.entry_id))
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    sync_status = _build_sync_status(result, entry_id=payload.entry_id)
    return FromFplResponse(
        squad=saved,
        player_names=result.player_names,
        entry_name=result.entry_name,
        gameweek=result.gameweek,
        sync_status=sync_status,
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
    # Never cache responses that are specific to a session.
    response.headers["Cache-Control"] = "no-store"
    sync_status = _build_sync_status(result, entry_id=pending_before.entry_id)
    return FromFplResponse(
        squad=saved,
        player_names=result.player_names,
        entry_name=result.entry_name,
        gameweek=result.gameweek,
        sync_status=sync_status,
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
