"""Phase 15.0 — Live prediction provider: the documented fallback chain.

Replaces the hardcoded :class:`StaticPredictionProvider` stub (5.5 xPTS for
everyone) in production. Every prediction produced here carries a ``source``
and ``data_quality`` label so the UI can never present a heuristic as a
computed model output.

Fallback chain (first available level wins for the whole gameweek):

1. ``model-backtest``      — the latest *successful* backtest run's stored
                             player predictions for the requested gameweek.
                             ``data_quality="historical-backtest"``.
2. ``baseline-model``      — Phase 5 recent-form baselines computed over
                             ingested ``PlayerGameweekPerformance`` history
                             (weighted last-3/5/10 form). Requires meaningful
                             coverage of the player universe.
                             ``data_quality="ingested-gameweek-history"``.
3. ``pre-season-proxy-v2`` — transparent heuristic: FPL price percentile base
                             rate, enriched with Understat last-season
                             xG/xA-per-90 + minutes share (offline snapshot),
                             a small labelled market-probability bump (The
                             Odds API, optional key) and a small negative
                             adjustment ONLY under severe forecast weather
                             (Open-Meteo).
                             ``data_quality="heuristic-proxy-enriched"``.

The static stub is NEVER used inside this provider; it exists only behind the
explicit ``PREDICTION_PROVIDER=static`` switch (tests / dry-run) and production
startup refuses that combination outright.

All enrichment connectors degrade gracefully: any failure simply removes that
signal and is recorded in :meth:`chain_meta` — a decisions request never fails
because of an upstream enrichment problem.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fpl_intelligence.data_providers.odds_api import OddsApiConnector
from fpl_intelligence.data_providers.open_meteo import OpenMeteoConnector
from fpl_intelligence.data_providers.understat import (
    UnderstatConnector,
    build_stats_from_row,
)
from fpl_intelligence.optimization.provider import PlayerPrediction

logger = logging.getLogger(__name__)

SOURCE_BACKTEST = "model-backtest"
SOURCE_BASELINE = "baseline-model"
SOURCE_PROXY = "pre-season-proxy-v2"
#: Phase 20.1 — precomputed by the daily cron, served from ``predictions_current``.
SOURCE_MATERIALIZED = "materialized-chain"

QUALITY_BACKTEST = "historical-backtest"
QUALITY_BASELINE = "ingested-gameweek-history"
QUALITY_PROXY = "heuristic-proxy-enriched"
QUALITY_MATERIALIZED = "precomputed-daily-materialize"

#: Human-readable chain labels surfaced in the dashboard banner/badges.
SOURCE_LABELS: dict[str, str] = {
    SOURCE_BACKTEST: "Backtest model",
    SOURCE_BASELINE: "Baseline model (2025/26 features)",
    SOURCE_PROXY: "Pre-season proxy v2 (price + fixtures + xG + market)",
    SOURCE_MATERIALIZED: "Materialized daily predictions (cron 06:10)",
}

#: Phase 20.1 — a materialized level must cover at least this many players to
#: be trusted over the inline chain.
MATERIALIZED_MIN_COVERAGE = 50
#: ...and be no older than this (the daily cron refreshes anyway).
MATERIALIZED_MAX_AGE_SECONDS = 36 * 3600.0

#: Minimum fraction of the player universe needing ingested history before
#: the baseline level is considered trustworthy.
BASELINE_COVERAGE_THRESHOLD = 0.25

#: Proxy tuning constants (documented, deterministic, no hidden randomness).
PROXY_PRICE_EXPONENT = 1.7
PROXY_PRICE_SCALE = 6.2
PROXY_PRICE_BASE = 0.8
PROXY_XG_WEIGHT = 1.05
PROXY_XA_WEIGHT = 0.75
PROXY_UNDERSTAT_CAP = 3.0
PROXY_MARKET_BUMP = 0.4
PROXY_XPTS_MIN = 0.4
PROXY_XPTS_MAX = 13.0

#: FPL team-name variants -> canonical The-Odds-API style names. Applied to
#: whatever name the DB gives us so h2h books match without hardcoding ids.
#: Phase 21.1: the table lives in data_providers.team_aliases and now covers
#: abbreviations ("MCI") too — re-exported here for backward compatibility.
from fpl_intelligence.data_providers.team_aliases import canonical_team_name  # noqa: E402


def _normalise_team_name(name: str | None) -> str:
    """Map an FPL display team name onto bookmaker-style naming."""
    return canonical_team_name(name)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class LabeledPlayerPrediction(PlayerPrediction):
    """A :class:`PlayerPrediction` carrying its chain provenance.

    ``source`` is one of :data:`SOURCE_BACKTEST`/:data:`SOURCE_BASELINE`/
    :data:`SOURCE_PROXY`; ``data_quality`` describes the evidence tier behind
    the number. The optimizer consumes the base fields unchanged — the labels
    ride along for the API/UI layer.
    """

    source: str = ""
    data_quality: str = ""


# ---------------------------------------------------------------------------
# Offline player catalog (committed seed = single source of truth for prices)
# ---------------------------------------------------------------------------


def load_player_catalog(
    path: Path | None = None,
) -> dict[int, dict[str, Any]]:
    """Load ``{element_id: {...}}`` from the committed bootstrap seed.

    The seed is regenerated in one pass from one live FPL bootstrap fetch by
    ``scripts/regenerate_bootstrap_seed.py`` (id, web_name, code,
    now_cost/10, team short name, position), so prices/teams always match the
    official API. Missing file or malformed rows simply yield an empty
    catalog (the proxy level then degrades to a flat conservative rate).
    """
    resolved = (path or Path("data") / "seed" / "fpl_bootstrap_seed.json").resolve()
    if not resolved.is_file():
        logger.warning("FPL bootstrap seed missing at %s", resolved)
        return {}
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("FPL bootstrap seed unreadable (%s)", exc)
        return {}
    teams = {
        int(t.get("id", 0)): str(t.get("short_name", ""))
        for t in raw.get("teams", [])
        if isinstance(t, dict)
    }
    catalog: dict[int, dict[str, Any]] = {}
    for row in raw.get("players", []):
        try:
            pid = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        price_tenths = row.get("now_cost")
        catalog[pid] = {
            "web_name": str(row.get("web_name", "")),
            "price": (float(price_tenths) / 10.0) if price_tenths is not None else 0.0,
            "position": int(row["position"]) if row.get("position") is not None else 0,
            "team": int(row["team"]) if row.get("team") is not None else 0,
            "team_short": teams.get(int(row["team"]) if row.get("team") is not None else 0, ""),
            # Phase 22 (D1): ownership share for the selected-by chips.
            "selected_by_percent": row.get("selected_by_percent"),
        }
    return catalog


# ---------------------------------------------------------------------------
# Chain levels
# ---------------------------------------------------------------------------


@dataclass
class ChainLevel:
    """One resolved level of the fallback chain for a gameweek."""

    source: str
    data_quality: str
    #: player_id -> expected points
    points: dict[int, float]
    #: player_ids present at this level (coverage evidence)
    covered: int
    #: free-form provenance notes (run id, row counts, disabled signals...)
    notes: dict[str, Any] = field(default_factory=dict)
    #: Optional per-player estimate extras for the proxy level:
    #: ``{player_id: {"minutes": .., "start": .., "conf": .., "compl": ..}}``
    per_player: dict[int, dict[str, float]] = field(default_factory=dict)

    def meta(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "data_quality": self.data_quality,
            "covered_players": self.covered,
            "notes": self.notes,
        }


def _distribution_for(expected_points: float, seed: int) -> np.ndarray:
    """Deterministic sampled points distribution around ``expected_points``.

    A right-skewed mixture (most weeks near the mean, occasional hauls) so
    floor/ceiling percentiles are meaningful for the optimizer.
    """
    rng = np.random.default_rng(seed)
    base = rng.normal(loc=expected_points, scale=max(0.6, expected_points * 0.55), size=1800)
    hauls = rng.normal(loc=expected_points + 6.0, scale=2.5, size=200)
    samples = np.concatenate([base, hauls])
    return np.clip(samples, 0.0, None)


def _make_prediction(
    player_id: int,
    gameweek: int,
    expected_points: float,
    *,
    expected_minutes: float,
    start_probability: float,
    source: str,
    data_quality: str,
    confidence: float,
    data_completeness: float,
) -> LabeledPlayerPrediction:
    points = _clamp(round(float(expected_points), 3), 0.0, 30.0)
    samples = _distribution_for(points, seed=player_id * 1000 + gameweek)
    return LabeledPlayerPrediction(
        player_id=player_id,
        gameweek=gameweek,
        expected_points=points,
        expected_minutes=round(float(expected_minutes), 1),
        start_probability=round(_clamp(float(start_probability), 0.0, 1.0), 3),
        distribution=samples,
        floor=round(float(np.percentile(samples, 10)), 3),
        ceiling=round(float(np.percentile(samples, 90)), 3),
        confidence=round(_clamp(float(confidence), 0.0, 1.0), 3),
        data_completeness=round(_clamp(float(data_completeness), 0.0, 1.0), 3),
        source=source,
        data_quality=data_quality,
    )


# ---------------------------------------------------------------------------
# Level 1 — model-backtest: latest successful run's stored gameweek predictions
# ---------------------------------------------------------------------------


def _backtest_points_for_gameweek(db: Session, gameweek: int) -> dict[int, float] | None:
    """Read the latest successful backtest run's predictions for ``gameweek``.

    Prefers the per-gameweek ``BacktestGameweekResult.predictions`` JSON
    (``{"<player_id>": <points|{"expected_points": ...}>}``); falls back to the
    latest-cutoff ``PlayerPrediction`` rows of that run. Returns ``None`` when
    no successful run covers the gameweek.
    """
    from fpl_intelligence.backtesting.models import (
        BacktestGameweekResult,
        BacktestRun,
    )

    run_id = db.execute(
        select(BacktestRun.id)
        .where(BacktestRun.status == "completed")
        .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run_id is None:
        return None

    row = db.execute(
        select(BacktestGameweekResult)
        .where(
            BacktestGameweekResult.run_id == run_id,
            BacktestGameweekResult.gameweek == gameweek,
        )
        .order_by(BacktestGameweekResult.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is not None and isinstance(row.predictions, dict) and row.predictions:
        points: dict[int, float] = {}
        for key, value in row.predictions.items():
            try:
                pid = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                value = value.get("expected_points", value.get("xpts"))
            try:
                points[pid] = float(value)
            except (TypeError, ValueError):
                continue
        if points:
            return points

    # Fallback: raw player_predictions rows of the run (latest cutoff per player).
    from fpl_intelligence.backtesting.models import PlayerPrediction as BacktestPlayerPrediction

    rows = (
        db.execute(
            select(BacktestPlayerPrediction)
            .where(BacktestPlayerPrediction.run_id == run_id)
            .order_by(BacktestPlayerPrediction.cutoff.desc())
        )
        .scalars()
        .all()
    )
    by_player: dict[int, float] = {}
    for r in rows:
        if r.predicted_expected_points is None:
            continue
        by_player.setdefault(int(r.player_id), float(r.predicted_expected_points))
    return by_player or None


# ---------------------------------------------------------------------------
# Level 2 — baseline-model: Phase 5 recent-form baselines on ingested history
# ---------------------------------------------------------------------------


def _baseline_points_for_gameweek(db: Session, gameweek: int) -> ChainLevel | None:
    """Weighted recent-form baselines computed over ingested GW performances.

    This is the Phase 5 recent-form baseline applied directly to the ingested
    ``PlayerGameweekPerformance`` table (leakage-safe: only gameweeks strictly
    before ``gameweek`` are used). Per player over the last three available
    gameweeks (recency weights 1.0 / 0.7 / 0.45):

        xPTS = weighted_mean(total_points) * minutes_share

    where ``minutes_share`` is the player's mean minutes/90 across the window
    (clamped to [0.30, 1.00]). Returns ``None`` when coverage of the player
    universe is below :data:`BASELINE_COVERAGE_THRESHOLD` — the chain then
    falls through to the transparent proxy instead of publishing thin data.
    """
    from fpl_intelligence.db.models import Gameweek, PlayerGameweekPerformance

    rows = db.execute(
        select(PlayerGameweekPerformance, Gameweek.provider_event_id)
        .join(Gameweek, PlayerGameweekPerformance.gameweek_id == Gameweek.id)
        .where(Gameweek.provider_event_id < gameweek)
        .order_by(Gameweek.provider_event_id.desc())
        .limit(60000)
    ).all()

    if not rows:
        return None

    latest_gw_seen = max(int(gw) for _, gw in rows)
    window = [latest_gw_seen, latest_gw_seen - 1, latest_gw_seen - 2]
    weights = {latest_gw_seen: 1.0, latest_gw_seen - 1: 0.7, latest_gw_seen - 2: 0.45}

    per_player: dict[int, list[tuple[int, int, float]]] = {}
    for perf, gw_id in rows:
        if gw_id not in weights:
            continue
        minutes = int(perf.minutes or 0)
        per_player.setdefault(int(perf.player_id), []).append(
            (int(gw_id), minutes, float(perf.total_points or 0.0))
        )

    points: dict[int, float] = {}
    for pid, entries in per_player.items():
        # One entry per gameweek (dedupe defensively, latest wins).
        by_gw: dict[int, tuple[int, float]] = {}
        for gw_id, minutes, pts in entries:
            by_gw[gw_id] = (minutes, pts)
        usable = [(gw, *by_gw[gw]) for gw in weights if gw in by_gw]
        if not usable:
            continue
        w_sum = sum(weights[gw] for gw, _, _ in usable)
        if w_sum <= 0:
            continue
        weighted_pts = sum(weights[gw] * pts for gw, _, pts in usable) / w_sum
        minutes_share = _clamp(
            sum(minutes for _, minutes, _ in usable) / (90.0 * len(usable)),
            0.30,
            1.00,
        )
        points[pid] = round(max(0.0, weighted_pts * minutes_share), 3)

    if not points:
        return None

    # Coverage gate: the level must explain a meaningful fraction of the
    # ingested player universe before it is published — thin history falls
    # through to the transparent proxy instead of serving sparse numbers.
    from fpl_intelligence.db.models import Player

    universe = int(db.scalar(select(func.count(Player.id))) or 0)
    coverage = (len(points) / universe) if universe > 0 else 0.0
    if coverage < BASELINE_COVERAGE_THRESHOLD:
        logger.info(
            "baseline-model skipped: coverage %.2f < %.2f (%d/%d players)",
            coverage,
            BASELINE_COVERAGE_THRESHOLD,
            len(points),
            universe,
        )
        return None

    return ChainLevel(
        source=SOURCE_BASELINE,
        data_quality=QUALITY_BASELINE,
        points=points,
        covered=len(points),
        notes={
            "window_gameweeks": window,
            "weights": [1.0, 0.7, 0.45],
            "players_with_history": len(per_player),
            "universe_players": universe,
            "coverage": round(coverage, 3),
            #: Phase 19.0 — the newest ingested gameweek feeding the form
            #: window; surfaced as "through GW{n}" in the chain label so users
            #: can see predictions refresh after every history-push.
            "through_gw": latest_gw_seen,
        },
    )


# ---------------------------------------------------------------------------
# Level 3 — pre-season-proxy-v2: transparent price/xG/market/weather heuristic
# ---------------------------------------------------------------------------

#: Conservative flat xPTS used when even the offline price catalog is missing.
PROXY_FLAT_RATE = 2.0
#: xPTS given to players a resolved level does not explicitly cover.
UNCOVERED_DEFAULT_XPTS = 1.0

#: Neutral estimate defaults applied when a level carries no per-player extras.
_LEVEL_DEFAULTS: dict[str, dict[str, float]] = {
    SOURCE_BACKTEST: {"minutes": 60.0, "start": 0.75, "conf": 0.70, "compl": 0.90},
    SOURCE_BASELINE: {"minutes": 60.0, "start": 0.75, "conf": 0.60, "compl": 0.80},
    SOURCE_PROXY: {"minutes": 55.0, "start": 0.70, "conf": 0.40, "compl": 0.55},
}


def _percentile_ranks(catalog: dict[int, dict[str, Any]]) -> dict[int, float]:
    """Deterministic price percentile in [0, 1] per player across the catalog.

    Ties are broken by player id so ranks never depend on dict ordering.
    """
    priced = sorted(
        ((pid, float(row.get("price") or 0.0)) for pid, row in catalog.items()),
        key=lambda item: (item[1], item[0]),
    )
    n = len(priced)
    if n <= 1:
        return {pid: 0.5 for pid, _ in priced}
    return {pid: idx / (n - 1) for idx, (pid, _) in enumerate(priced)}


def _fixtures_for_gameweek(db: Session, gameweek: int) -> list[dict[str, int]]:
    """Resolve ``[{home_team_id, away_team_id}]`` for a provider gameweek."""
    from fpl_intelligence.db.models import Fixture, Gameweek

    rows = db.execute(
        select(Fixture.home_team_id, Fixture.away_team_id)
        .join(Gameweek, Fixture.gameweek_id == Gameweek.id)
        .where(Gameweek.provider_event_id == gameweek)
    ).all()
    return [{"home_team_id": int(home), "away_team_id": int(away)} for home, away in rows]


def _team_names(db: Session) -> dict[int, str]:
    """``{team_id: display_name}`` from the teams table (empty when absent)."""
    from fpl_intelligence.db.models import Team

    try:
        rows = db.execute(select(Team.id, Team.name)).all()
    except Exception:  # noqa: BLE001 - names are enrichment, never a dependency
        return {}
    return {int(tid): str(name) for tid, name in rows}


def _market_probs_for_fixtures(
    fixtures: list[dict[str, int]],
    team_names: dict[int, str],
    matches: list[Any],
) -> tuple[dict[int, float], list[dict[str, Any]]]:
    """Map team_id -> favourite win-probability for every matched fixture.

    A fixture is *matched* when both sides resolve against one h2h book via
    normalised team names. Only the market favourite's players receive the
    labelled bump; the underdog gets nothing.
    """
    probs: dict[int, float] = {}
    detail: list[dict[str, Any]] = []
    for fx in fixtures:
        home_name = _normalise_team_name(team_names.get(fx["home_team_id"], ""))
        away_name = _normalise_team_name(team_names.get(fx["away_team_id"], ""))
        if not home_name or not away_name:
            continue
        for match in matches:
            home_p = match.prob_for_team(home_name)
            away_p = match.prob_for_team(away_name)
            if home_p is None or away_p is None:
                continue
            if home_p >= away_p:
                probs[fx["home_team_id"]] = home_p
                favourite, value = home_name, home_p
            else:
                probs[fx["away_team_id"]] = away_p
                favourite, value = away_name, away_p
            detail.append(
                {
                    "home": home_name,
                    "away": away_name,
                    "favourite": favourite,
                    "favourite_prob": round(value, 3),
                }
            )
            break
    return probs, detail


def _weather_adjustments_for_fixtures(
    fixtures: list[dict[str, int]],
    weather: OpenMeteoConnector | None,
) -> tuple[dict[int, float], list[str]]:
    """Apply severe-weather adjustments to both sides of affected fixtures.

    The stadium belongs to the *home* team; a severe forecast there penalises
    both attacks equally (documented -0.3 from :mod:`open_meteo`).

    Per-stadium fetches are parallelised with a ThreadPoolExecutor so the worst-
    case latency is a single timeout (not N x timeout for N stadiums).
    """
    adjustments: dict[int, float] = {}
    reasons: list[str] = []
    if weather is None:
        return adjustments, reasons
    home_id_to_fixture: dict[int, dict[str, int]] = {}
    for fx in fixtures:
        home_id = fx["home_team_id"]
        if home_id not in home_id_to_fixture:
            home_id_to_fixture[home_id] = fx
    if not home_id_to_fixture:
        return adjustments, reasons

    def _fetch(home_id: int) -> tuple[int, Any]:
        return home_id, weather.fetch_matchday_outlook(home_id)

    with ThreadPoolExecutor(max_workers=min(len(home_id_to_fixture), 5)) as executor:
        futures = {executor.submit(_fetch, hid): hid for hid in home_id_to_fixture}
        for future in as_completed(futures):
            home_id, outlook = future.result()
            if outlook is None or outlook.severity != "severe":
                continue
            fx = home_id_to_fixture[home_id]
            adjustments[home_id] = outlook.adjustment
            adjustments[fx["away_team_id"]] = outlook.adjustment
            reasons.append(f"{outlook.stadium}: {outlook.reason}")
    return adjustments, reasons


def _proxy_points_for_gameweek(
    db: Session,
    gameweek: int,
    catalog: dict[int, dict[str, Any]],
    understat_index: dict[str, dict[str, Any]],
    *,
    odds: OddsApiConnector | None,
    weather: OpenMeteoConnector | None,
) -> ChainLevel:
    """Transparent heuristic — every term is documented and labelled.

    Formula per player::

        base = PROXY_PRICE_BASE + PROXY_PRICE_SCALE * pct ** PROXY_PRICE_EXPONENT
        x90  = min(PROXY_UNDERSTAT_CAP,
                   PROXY_XG_WEIGHT * xG90 + PROXY_XA_WEIGHT * xA90)  # snapshot
        pts  = base + x90 * minutes_share                            # threat
             + PROXY_MARKET_BUMP * p_win       # market favourites only
             + weather_adjustment              # severe forecasts only
        pts  = clamp(pts, PROXY_XPTS_MIN, PROXY_XPTS_MAX)

    Enrichment signals degrade independently: no odds key, unreachable weather,
    or an unmatched Understat name each simply drop their term and are recorded
    in the chain notes. The level only reports zero coverage when there is no
    player universe at all (empty seed catalog AND empty players table) — the
    caller then surfaces the chain as unavailable instead of inventing numbers.
    """
    from fpl_intelligence.db.models import Player

    notes: dict[str, Any] = {
        "formula": (
            "base(price_pct^e) + understat_x90*share + market_bump(favourite) + weather_adj(severe)"
        ),
        "catalog_players": len(catalog),
        "understat_snapshot_players": len(understat_index),
    }

    # --- player universe -------------------------------------------------------
    try:
        rows = db.execute(select(Player.id)).all()
        db_player_ids = {int(row[0]) for row in rows}
    except Exception as exc:  # noqa: BLE001 - DB trouble must not kill the proxy
        logger.warning("proxy universe query failed (%s); using seed catalog", exc)
        db_player_ids = set()
    universe = sorted(set(catalog.keys()) | db_player_ids)
    if not universe:
        notes["degraded"] = "no player universe (catalog empty and DB empty)"
        return ChainLevel(
            source=SOURCE_PROXY,
            data_quality=QUALITY_PROXY,
            points={},
            covered=0,
            notes=notes,
        )

    # --- flat-rate fallback when the price catalog is unusable ------------------
    if not catalog:
        notes["degraded"] = "flat conservative rate (bootstrap seed missing)"
        return ChainLevel(
            source=SOURCE_PROXY,
            data_quality=QUALITY_PROXY,
            points={pid: PROXY_FLAT_RATE for pid in universe},
            covered=len(universe),
            notes=notes,
        )

    # --- fixture + enrichment context -------------------------------------------
    fixtures = _fixtures_for_gameweek(db, gameweek)
    notes["fixtures_found"] = len(fixtures)

    team_names = _team_names(db)

    def _fetch_market() -> tuple[dict[int, float], list[dict[str, Any]]]:
        if odds is None or not odds.enabled or not fixtures:
            return {}, []
        _t0 = time.perf_counter()
        try:
            snapshot = odds.fetch_epl_odds()
            if snapshot is not None:
                probs, detail = _market_probs_for_fixtures(fixtures, team_names, snapshot.matches)
                return probs, detail
        except Exception:  # noqa: BLE001 - graceful degradation contract
            pass
        logger.warning("proxy market fetch %.3fs (degraded)", time.perf_counter() - _t0)
        return {}, []

    def _fetch_weather() -> tuple[dict[int, float], list[str]]:
        if not fixtures:
            return {}, []
        _t0 = time.perf_counter()
        result = _weather_adjustments_for_fixtures(fixtures, weather)
        logger.info("proxy weather fetch %.3fs", time.perf_counter() - _t0)
        return result

    _t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as executor:
        market_future = executor.submit(_fetch_market)
        weather_future = executor.submit(_fetch_weather)
        market_probs, market_detail = market_future.result()
        weather_adj, weather_reasons = weather_future.result()
    logger.info("proxy enrichment total %.3fs", time.perf_counter() - _t0)
    notes["market_fixtures_matched"] = len(market_detail)
    notes["weather_severe_fixtures"] = weather_reasons

    # --- Understat per-90 context (offline snapshot; latest season wins) --------
    understat_by_name: dict[str, dict[str, float]] = {}
    max_minutes = 0.0
    for row in catalog.values():
        name = str(row.get("web_name", ""))
        hit = UnderstatConnector.match_player(understat_index, name)
        if hit is None:
            continue
        stats = build_stats_from_row(hit)
        minutes = max(0.0, float(stats.minutes))
        max_minutes = max(max_minutes, minutes)
        understat_by_name[name] = {
            "minutes": minutes,
            "x90": min(
                PROXY_UNDERSTAT_CAP,
                PROXY_XG_WEIGHT * stats.xg_per_90 + PROXY_XA_WEIGHT * stats.xa_per_90,
            ),
        }
    for entry in understat_by_name.values():
        share = entry["minutes"] / max_minutes if max_minutes > 0 else 0.0
        entry["share"] = _clamp(share, 0.0, 1.0)
    notes["understat_matched"] = len(understat_by_name)

    return _score_proxy_universe(
        universe=universe,
        catalog=catalog,
        understat_by_name=understat_by_name,
        market_probs=market_probs,
        weather_adj=weather_adj,
        notes=notes,
    )


def _score_proxy_universe(
    *,
    universe: list[int],
    catalog: dict[int, dict[str, Any]],
    understat_by_name: dict[str, dict[str, float]],
    market_probs: dict[int, float],
    weather_adj: dict[int, float],
    notes: dict[str, Any],
) -> ChainLevel:
    """Apply the documented proxy formula to every player in ``universe``."""
    pcts = _percentile_ranks(catalog)
    points: dict[int, float] = {}
    per_player: dict[int, dict[str, float]] = {}

    for pid in universe:
        row = catalog.get(pid)
        if row is None:
            # DB-only player without a seed row: conservative neutral number.
            points[pid] = UNCOVERED_DEFAULT_XPTS
            per_player[pid] = {
                "minutes": 30.0,
                "start": 0.45,
                "conf": 0.25,
                "compl": 0.35,
            }
            continue

        pct = pcts.get(pid, 0.0)
        base = PROXY_PRICE_BASE + PROXY_PRICE_SCALE * (pct**PROXY_PRICE_EXPONENT)

        und = understat_by_name.get(str(row.get("web_name", "")))
        x90 = float(und["x90"]) if und else 0.0
        share = float(und["share"]) if und else 0.0

        pts = base + x90 * share

        team_id = int(row.get("team") or 0)
        bump = PROXY_MARKET_BUMP * market_probs.get(team_id, 0.0)
        pts += bump + weather_adj.get(team_id, 0.0)

        est_share = (
            _clamp(0.45 * pct + 0.55 * share, 0.25, 0.95)
            if share > 0
            else _clamp(0.35 + 0.45 * pct, 0.25, 0.90)
        )
        confidence = 0.30 + (0.15 if und else 0.0) + (0.10 if bump > 0 else 0.0)
        completeness = 0.45 + (0.15 if und else 0.0) + (0.10 if market_probs else 0.05)

        points[pid] = round(_clamp(pts, PROXY_XPTS_MIN, PROXY_XPTS_MAX), 3)
        per_player[pid] = {
            "minutes": round(90.0 * est_share, 1),
            "start": round(_clamp(0.30 + 0.65 * est_share, 0.30, 0.95), 3),
            "conf": round(_clamp(confidence, 0.20, 0.60), 3),
            "compl": round(_clamp(completeness, 0.30, 0.70), 3),
            "breakdown": {
                "base": round(base, 2),
                "xg_xa_term": round(x90 * share, 2),
                "market_term": round(bump, 2),
                "weather_term": round(weather_adj.get(team_id, 0.0), 2),
            },
        }

    return ChainLevel(
        source=SOURCE_PROXY,
        data_quality=QUALITY_PROXY,
        points=points,
        covered=len(points),
        notes=notes,
        per_player=per_player,
    )


# ---------------------------------------------------------------------------
# Chain orchestrator — LivePredictionProvider
# ---------------------------------------------------------------------------


def _source_label(source: str, notes: dict[str, Any] | None = None) -> str:
    """Human label for a chain level; baseline appends its form cutoff."""
    label = SOURCE_LABELS.get(source, source)
    if source == SOURCE_BASELINE and notes:
        through = notes.get("through_gw")
        if through:
            label += f" · through GW{through}"
    return label


class PredictionUnavailableError(RuntimeError):
    """Raised when no chain level can serve predictions for a gameweek.

    This only happens when the player universe itself is empty (no seed
    catalog, no ingested players) — i.e. the deployment is missing its data
    seeds. It is deliberately *not* raised for enrichment failures.
    """


@dataclass
class PredictionChainResult:
    """The full fallback-chain outcome for one gameweek request."""

    gameweek: int
    #: every level that produced data, best-first
    levels: list[ChainLevel]
    #: the level whose numbers are actually being served (last of ``levels``)
    resolved: ChainLevel

    @property
    def source(self) -> str:
        return self.resolved.source

    @property
    def data_quality(self) -> str:
        return self.resolved.data_quality

    def meta(self) -> dict[str, Any]:
        """Serialisable provenance payload for the API/dashboard."""
        return {
            "source": self.resolved.source,
            "source_label": _source_label(self.resolved.source, self.resolved.notes),
            "data_quality": self.resolved.data_quality,
            "covered_players": self.resolved.covered,
            "levels_considered": len(self.levels),
            "notes": self.resolved.notes,
            "chain": [level.meta() for level in self.levels],
        }


def _resolve_odds_api_key() -> str:
    """Best-effort odds key lookup: settings object first, then environment.

    Kept defensive on purpose — a missing/broken settings module must never
    break prediction construction; it only disables market enrichment.
    """
    try:  # pragma: no cover - config wiring is covered by integration tests
        from fpl_intelligence.config import settings as _settings_module

        holder = getattr(_settings_module, "settings", None) or _settings_module
        api_key = str(getattr(holder, "the_odds_api_key", "") or "").strip()
        if api_key:
            return api_key
    except Exception:  # noqa: BLE001 - settings are optional at this layer
        pass
    return os.environ.get("THE_ODDS_API_KEY", "").strip()


class LivePredictionProvider:
    """DecisionPredictionProvider backed by the transparent fallback chain.

    Resolution order per gameweek:

    1. **model-backtest** — stored predictions from the latest successful
       backtest run (:func:`_backtest_points_for_gameweek`).
    2. **baseline-model** — recency-weighted ingested gameweek history behind
       a coverage gate (:func:`_baseline_points_for_gameweek`).
    3. **pre-season-proxy-v2** — transparent price/xG/market/weather heuristic
       (:func:`_proxy_points_for_gameweek`), available whenever seeds exist.

    Requests are served from the resolved level; individual players it does
    not cover fall back to the better levels. Players no level can speak
    about are omitted rather than invented.
    """

    def __init__(
        self,
        session: Session,
        *,
        catalog_path: Path | None = None,
        understat_snapshot_path: Path | None = None,
    ) -> None:
        self.session = session
        self._catalog_path = catalog_path
        self._understat_path = understat_snapshot_path
        self._catalog: dict[int, dict[str, Any]] | None = None
        self._understat_index: dict[str, dict[str, Any]] | None = None
        self._odds_connector: OddsApiConnector | None = None
        self._odds_error: str | None = None
        self._weather_connector: OpenMeteoConnector | None = None
        self._weather_error: str | None = None
        #: most recent :meth:`resolve_chain` outcome, for cheap meta re-reads
        self.last_result: PredictionChainResult | None = None
        #: Per-gameweek cache for :meth:`resolve_chain` results.
        #: Eliminates N+1 redundant chain resolutions when the optimizers call
        #: :meth:`get_player_prediction` hundreds of times for the same gameweek
        #: (one call per player). The cache lives for the provider instance
        #: lifetime (one request), so each fresh request sees a cold cache.
        self._chain_cache: dict[int, PredictionChainResult] = {}

    # -- lazily-built shared state ----------------------------------------------

    def player_catalog(self) -> dict[int, dict[str, Any]]:
        """Offline price/position catalog from the committed bootstrap seed."""
        if self._catalog is None:
            try:
                self._catalog = load_player_catalog(self._catalog_path)
            except Exception as exc:  # noqa: BLE001 - degrade to empty catalog
                logger.warning("player catalog unavailable: %s", exc)
                self._catalog = {}
        return self._catalog

    def understat_index(self) -> dict[str, dict[str, Any]]:
        """Name-indexed Understat snapshot rows (latest season wins).

        Phase 21.1 (T5): rows persisted by a successful masked refresh
        (``provider_refresh``) merge OVER the committed seed so 2026/27 xG/xA
        reaches the chain without redeploying the bundle.
        """
        if self._understat_index is None:
            try:
                snapshot = UnderstatConnector.load_snapshot(self._understat_path)
                self._understat_index = UnderstatConnector.snapshot_player_index(snapshot)
                self._merge_understat_refresh(self._understat_index)
            except Exception as exc:  # noqa: BLE001 - xG is enrichment only
                logger.warning("Understat index unavailable: %s", exc)
                self._understat_index = {}
        return self._understat_index

    def _merge_understat_refresh(self, index: dict[str, dict[str, Any]]) -> None:
        """Overlay the DB-stored refresh payload (best-effort, never raises).

        A missing table must ROLL BACK before returning — otherwise the
        request's Postgres transaction stays aborted and every later query in
        the request fails with InFailedSqlTransaction.
        """
        try:
            from sqlalchemy import text

            from fpl_intelligence.sync.materialized_models import ProviderRefreshDB

            # Self-sealing DDL: deployments on alembic <0019 get the table on
            # first use instead of erroring into an aborted transaction.
            self.session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS provider_refresh ("
                    " source VARCHAR(60) PRIMARY KEY,"
                    " season_label VARCHAR(40),"
                    " player_count INTEGER NOT NULL DEFAULT 0,"
                    " payload JSONB NOT NULL DEFAULT '[]'::jsonb,"
                    " fetched_at TIMESTAMP WITH TIME ZONE NOT NULL)"
                )
            )
            row = self.session.scalar(
                select(ProviderRefreshDB).where(ProviderRefreshDB.source == "understat")
            )
        except Exception as exc:  # noqa: BLE001 — table may be absent pre-migration
            self.session.rollback()
            logger.debug("provider_refresh read skipped: %s", exc)
            return
        if row is None or not isinstance(row.payload, list):
            return
        for raw in row.payload:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("player_name") or "").strip().lower()
            if name and raw.get("xG") is not None:
                index[name] = raw

    def _get_odds(self) -> OddsApiConnector | None:
        if self._odds_connector is None and self._odds_error is None:
            api_key = _resolve_odds_api_key()
            if not api_key:
                self._odds_error = "THE_ODDS_API_KEY not set"
            else:
                try:
                    self._odds_connector = OddsApiConnector(api_key=api_key)
                except Exception as exc:  # noqa: BLE001 - never fatal
                    self._odds_error = f"{type(exc).__name__}: {exc}"
        return self._odds_connector

    def _get_weather(self) -> OpenMeteoConnector | None:
        if self._weather_connector is None and self._weather_error is None:
            try:
                self._weather_connector = OpenMeteoConnector()
            except Exception as exc:  # noqa: BLE001 - never fatal
                self._weather_error = f"{type(exc).__name__}: {exc}"
        return self._weather_connector

    # -- chain resolution --------------------------------------------------------

    def _materialized_level(self, gameweek: int) -> ChainLevel | None:
        """Phase 20.1 — read the cron-precomputed level for ``gameweek``.

        Returns a :class:`ChainLevel` built purely from ``predictions_current``
        (one indexed query, zero network) when fresh rows cover enough of the
        universe; ``None`` otherwise so the inline chain takes over.
        """
        from datetime import timedelta

        try:
            from fpl_intelligence.sync.materialized_models import PredictionCurrentDB
        except ImportError:  # pragma: no cover — table missing on old deploys
            return None

        cutoff = datetime.now(UTC) - timedelta(seconds=MATERIALIZED_MAX_AGE_SECONDS)
        try:
            rows = self.session.execute(
                select(PredictionCurrentDB).where(
                    PredictionCurrentDB.gameweek == int(gameweek),
                    PredictionCurrentDB.computed_at >= cutoff,
                )
            ).scalars().all()
        except Exception as exc:  # noqa: BLE001 — fall back to the inline chain
            logger.warning("materialized level query failed: %s", exc)
            return None

        if len(rows) < MATERIALIZED_MIN_COVERAGE:
            return None

        points: dict[int, float] = {}
        per_player: dict[int, dict[str, float]] = {}
        for row in rows:
            pid = int(row.element_id)
            points[pid] = float(row.expected_points)
            extras: dict[str, float] = {"conf": 0.75, "compl": 0.85}
            if row.minutes_estimate is not None:
                extras["minutes"] = float(row.minutes_estimate)
            if row.start_prob is not None:
                extras["start"] = float(row.start_prob)
            per_player[pid] = extras

        return ChainLevel(
            source=SOURCE_MATERIALIZED,
            data_quality=QUALITY_MATERIALIZED,
            points=points,
            covered=len(points),
            notes={
                "computed_at": max(r.computed_at for r in rows).isoformat(),
                "origin": "daily materialize cron (06:10 UTC)",
            },
            per_player=per_player,
        )

    def resolve_chain(self, gameweek: int) -> PredictionChainResult:
        """Run every level best-first and return the full chain outcome.

        Raises :class:`PredictionUnavailableError` when no level can
        produce any number at all (empty universe).

        Results are cached per-gameweek for the lifetime of this provider
        instance. Repeated calls for the same gameweek (common during
        optimization, where :meth:`get_player_prediction` is invoked once per
        player) return the cached result instead of re-running the entire
        chain — eliminating the N+1 redundancy that caused 504 timeouts.
        """
        if gameweek in self._chain_cache:
            return self._chain_cache[gameweek]

        _t_start = time.perf_counter()

        # Phase 20.1 — materialized fast path: the daily cron already ran the
        # full chain; serve it from one indexed query with zero network I/O.
        # This is what keeps every prod data call under 2s.
        materialized = self._materialized_level(gameweek)
        if materialized is not None and materialized.points:
            result = PredictionChainResult(
                gameweek=gameweek, levels=[materialized], resolved=materialized
            )
            self.last_result = result
            logger.info(
                "resolve_chain gw=%d: served from materialized table %.3fs (%d players)",
                gameweek,
                time.perf_counter() - _t_start,
                materialized.covered,
            )
            self._chain_cache[gameweek] = result
            return result

        catalog = self.player_catalog()
        logger.info(
            "resolve_chain gw=%d: player_catalog %.3fs", gameweek, time.perf_counter() - _t_start
        )

        _t0 = time.perf_counter()
        understat_index = self.understat_index()
        logger.info(
            "resolve_chain gw=%d: understat_index %.3fs", gameweek, time.perf_counter() - _t0
        )

        levels: list[ChainLevel] = []

        # Level 1 — model-backtest (raw points dict; wrapped here).
        try:
            backtest_points = _backtest_points_for_gameweek(self.session, gameweek)
        except Exception as exc:  # noqa: BLE001 - levels are independent
            logger.warning("Level 1 model-backtest failed: %s", exc)
            backtest_points = None
        if backtest_points:
            levels.append(
                ChainLevel(
                    source=SOURCE_BACKTEST,
                    data_quality=QUALITY_BACKTEST,
                    points=backtest_points,
                    covered=len(backtest_points),
                    notes={"origin": "latest successful backtest run"},
                )
            )

        # Level 2 — baseline-model (already a ChainLevel with coverage gate).
        try:
            baseline_level = _baseline_points_for_gameweek(self.session, gameweek)
        except Exception as exc:  # noqa: BLE001 - levels are independent
            logger.warning("Level 2 baseline-model failed: %s", exc)
            baseline_level = None
        if baseline_level is not None and baseline_level.points:
            levels.append(baseline_level)

        # Level 3 — pre-season-proxy-v2 (always attempted as the floor).
        _t0 = time.perf_counter()
        try:
            proxy_level = _proxy_points_for_gameweek(
                self.session,
                gameweek,
                catalog,
                understat_index,
                odds=self._get_odds(),
                weather=self._get_weather(),
            )
        except Exception as exc:  # noqa: BLE001 - levels are independent
            logger.warning("Level 3 pre-season-proxy-v2 failed: %s", exc)
            proxy_level = None
        logger.info("resolve_chain gw=%d: proxy_level %.3fs", gameweek, time.perf_counter() - _t0)
        if proxy_level is not None and proxy_level.points:
            levels.append(proxy_level)

        if not levels:
            raise PredictionUnavailableError(
                f"No prediction level could serve gameweek {gameweek}: "
                "no backtest run, no ingested history above the coverage "
                "threshold, and an empty proxy universe (missing seeds?)."
            )

        result = PredictionChainResult(gameweek=gameweek, levels=levels, resolved=levels[0])
        self.last_result = result
        logger.info(
            "resolve_chain gw=%d: total %.3fs (source=%s)",
            gameweek,
            time.perf_counter() - _t_start,
            result.source,
        )
        self._chain_cache[gameweek] = result
        return result

    def _label_predictions(
        self,
        result: PredictionChainResult,
        player_ids: list[int],
    ) -> dict[int, PlayerPrediction]:
        """Convert resolved chain numbers into labelled player predictions."""
        resolved = result.resolved
        defaults = _LEVEL_DEFAULTS.get(
            resolved.source,
            {"minutes": 55.0, "start": 0.70, "conf": 0.50, "compl": 0.60},
        )
        better_levels = [lvl for lvl in result.levels if lvl is not resolved]

        predictions: dict[int, PlayerPrediction] = {}
        for pid in player_ids:
            xp = resolved.points.get(pid)
            extras = resolved.per_player.get(pid, {})
            source, quality = resolved.source, resolved.data_quality

            if xp is None:
                # Resolved level can't speak for this player: try better ones.
                for level in reversed(better_levels):
                    hit = level.points.get(pid)
                    if hit is not None:
                        xp = hit
                        extras = level.per_player.get(pid, {})
                        source, quality = level.source, level.data_quality
                        break
                else:
                    continue  # truly uncovered — omit rather than invent

            predictions[pid] = _make_prediction(
                pid,
                result.gameweek,
                float(xp),
                expected_minutes=float(extras.get("minutes", defaults["minutes"])),
                start_probability=float(extras.get("start", defaults["start"])),
                source=source,
                data_quality=quality,
                confidence=float(extras.get("conf", defaults["conf"])),
                data_completeness=float(extras.get("compl", defaults["compl"])),
            )
        return predictions

    # -- DecisionPredictionProvider protocol -------------------------------------

    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        """Serve one player/gameweek pair through the chain."""
        preds = self.get_squad_predictions([player_id], [gameweek])
        prediction = preds.get(int(gameweek), {}).get(int(player_id))
        if prediction is None:
            raise PredictionUnavailableError(
                f"No chain level covers player {player_id} in gameweek {gameweek}."
            )
        return prediction

    def get_squad_predictions(
        self,
        player_ids: list[int],
        gameweeks: list[int],
    ) -> dict[int, dict[int, PlayerPrediction]]:
        """Serve every requested (gameweek, player) pair through the chain.

        Returns ``{gameweek: {player_id: LabeledPlayerPrediction}}``. Players
        that no level covers are simply absent from the inner maps.
        """
        wanted_ids = [int(pid) for pid in player_ids]
        result: dict[int, dict[int, PlayerPrediction]] = {}
        for gw in sorted({int(gw) for gw in gameweeks}):
            chain_result = self.resolve_chain(gw)
            result[gw] = self._label_predictions(chain_result, wanted_ids)
        return result

    def get_all_predictions(self, gameweek: int) -> dict[int, PlayerPrediction]:
        """Serve every player the chain can speak for in ``gameweek``.

        Used by the chip simulator (Free Hit / Wildcard) to rank the full pool.
        The universe is the seed catalog extended with any players known to the
        database — the same set the proxy level scores. Players no level covers
        are absent rather than invented.
        """
        catalog = self.player_catalog()
        try:
            from fpl_intelligence.db.models import Player

            db_rows = self.session.execute(select(Player.id)).all()
            db_ids = {int(row[0]) for row in db_rows}
        except Exception as exc:  # noqa: BLE001 - degrade to catalog-only universe
            logger.warning("get_all_predictions universe query failed: %s", exc)
            db_ids = set()
        universe = sorted(set(catalog.keys()) | db_ids)
        if not universe:
            return {}
        chain_result = self.resolve_chain(int(gameweek))
        return self._label_predictions(chain_result, universe)

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        """Return the number of fixtures ``player_id``'s team has in ``gameweek``.

        Drives the chip simulator's Bench Boost / Triple Captain logic: a player
        on a blank (0) or double (2+) gameweek is treated differently. When the
        player's team or the gameweek's fixtures are unknown we conservatively
        return 1 — the common single-fixture case — rather than invent a number.
        """
        from fpl_intelligence.db.models import (
            Fixture,
            Gameweek,
            PlayerTeamMembership,
        )

        # Resolve the internal gameweek row id from the provider_event_id, which
        # is what the FPL-facing gameweek number maps to.
        gw_row = self.session.execute(
            select(Gameweek.id).where(Gameweek.provider_event_id == int(gameweek))
        ).scalar_one_or_none()
        if gw_row is None:
            return 1

        # The player's current team in the most recent membership row.
        membership = self.session.execute(
            select(PlayerTeamMembership.team_id)
            .where(PlayerTeamMembership.player_id == int(player_id))
            .order_by(PlayerTeamMembership.valid_from.desc().nulls_last())
            .limit(1)
        ).scalar_one_or_none()
        if membership is None:
            return 1

        try:
            rows = self.session.execute(
                select(Fixture.id).where(
                    Fixture.gameweek_id == gw_row,
                    Fixture.postponed.is_(False),
                    (Fixture.home_team_id == membership) | (Fixture.away_team_id == membership),
                )
            ).all()
            count = len(rows)
        except Exception as exc:  # noqa: BLE001 - conservative default
            logger.warning("get_fixture_count query failed: %s", exc)
            return 1
        return count if count > 0 else 1

    # -- provenance helpers for the API/dashboard layer ---------------------------

    def chain_meta(self, gameweek: int) -> dict[str, Any]:
        """Provenance payload for a gameweek (reuses the last resolution)."""
        if self.last_result is None or self.last_result.gameweek != int(gameweek):
            self.resolve_chain(gameweek)
        assert self.last_result is not None  # narrow for type-checkers
        meta = self.last_result.meta()
        if self._odds_error:
            meta["market_check"] = {"enabled": False, "reason": self._odds_error}
        else:
            # Find the proxy level in the chain to report market_check status.
            proxy_level = next(
                (lvl for lvl in self.last_result.levels if lvl.source == SOURCE_PROXY),
                None,
            )
            if proxy_level is not None:
                matched = proxy_level.notes.get("market_fixtures_matched")
                if matched:
                    meta["market_check"] = {
                        "enabled": True,
                        "fixtures_matched": matched,
                    }
                else:
                    # Proxy ran but matched zero fixtures — report honestly
                    # instead of "agrees (0 fixtures)" (E4).
                    meta["market_check"] = {
                        "enabled": False,
                        "reason": "no fixtures matched yet",
                    }
            else:
                meta["market_check"] = {
                    "enabled": False,
                    "reason": "no fixtures matched yet",
                }
        return meta
