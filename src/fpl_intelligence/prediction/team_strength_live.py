"""Live activation of holdout-approved Team Strength EWMA.

Stage 2 locked holdout (2025-26) approved:

* method ``ewma``
* window ``5``
* decay ``0.9``
* model version ``2.0.0``
* feature version ``team-strength-2.0.0``

This module is the **runtime** bridge. It never tunes hyperparameters from
live data. When team-match history is missing it returns neutral multipliers
and reports status unavailable so the chain stays honest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Frozen holdout decision — do not change without a new locked evaluation.
TS_METHOD = "ewma"
TS_WINDOW = 5
TS_DECAY = 0.9
TS_MODEL_NAME = "team_strength"
TS_MODEL_VERSION = "2.0.0"
TS_FEATURE_VERSION = "team-strength-2.0.0"
TS_LEAGUE_AVG_GOALS = 1.4

# Conservative clamps so TS modulates the live chain without dominating it.
TS_MULT_MIN = 0.78
TS_MULT_MAX = 1.28
TS_BLANK_MULT = 1.0


@dataclass(frozen=True)
class TeamStrengthLiveResult:
    """Per-team xPTS multipliers plus provenance for the chain banner."""

    multipliers: dict[int, float]
    notes: dict[str, Any]

    @property
    def applied(self) -> bool:
        return bool(self.notes.get("applied"))


def _neutral_notes(reason: str) -> dict[str, Any]:
    return {
        "applied": False,
        "status": "unavailable",
        "reason": reason,
        "method": TS_METHOD,
        "window": TS_WINDOW,
        "decay": TS_DECAY,
        "model_name": TS_MODEL_NAME,
        "model_version": TS_MODEL_VERSION,
        "feature_version": TS_FEATURE_VERSION,
        "teams_estimated": 0,
        "fixtures_used": 0,
    }


def compute_team_strength_multipliers(
    db: Session,
    gameweek: int,
    *,
    cutoff_time: datetime | None = None,
) -> TeamStrengthLiveResult:
    """Compute fixture-relative xPTS multipliers from EWMA team strength.

    For each fixture in the current-season gameweek, expected goals (lambda)
    for each side are derived from holdout-approved EWMA strengths. The
    multiplier is ``lambda / league_average``, clamped to a conservative band.
    """
    cutoff = cutoff_time or datetime.now(UTC)
    try:
        from fpl_intelligence.prediction.gameweek_resolve import resolve_gameweek_id
        from fpl_intelligence.prediction.team_strength_engine import TeamStrengthEngine
        from fpl_intelligence.db.models import Fixture
        from sqlalchemy import select
    except Exception as exc:  # noqa: BLE001
        logger.warning("team strength live import failed: %s", exp)
        return TeamStrengthLiveResult({}, _neutral_notes(f"import_failed:{type(exc).__name__}"))

    try:
        gw_id = resolve_gameweek_id(db, int(gameweek))
    except Exception as exc:  # noqa: BLE001
        logger.warning("team strength gameweek resolve failed: %s", exc)
        return TeamStrengthLiveResult({}, _neutral_notes(f"gameweek_resolve:{type(exc).__name__}"))

    if gw_id is None:
        return TeamStrengthLiveResult({}, _neutral_notes("gameweek_not_found"))

    try:
        fixtures = db.execute(
            select(
                Fixture.id,
                Fixture.home_team_id,
                Fixture.away_team_id,
                Fixture.postponed,
            ).where(Fixture.gameweek_id == gw_id)
        ).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("team strength fixture query failed: %s", exc)
        return TeamStrengthLiveResult({}, _neutral_notes(f"fixture_query:{type(exc).__name__}"))

    active = [f for f in fixtures if not bool(getattr(f, "postponed", False))]
    if not active:
        return TeamStrengthLiveResult({}, _neutral_notes("no_fixtures"))

    try:
        engine = TeamStrengthEngine.from_db(db, league_average_goals=TS_LEAGUE_AVG_GOALS)
    except Exception as exc:  # noqa: BLE001
        exp = type(exc).__name__
        logger.warning("team strength from_db failed: %s", exp)
        return TeamStrengthLiveResult({}, _neutral_notes(f"from_db:{exp}"))

    try:
        estimates = engine.estimate_all(
            cutoff, method=TS_METHOD, window=TS_WINDOW, decay=TS_DECAY
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("team strength estimate_all failed: %s", exc)
        return TeamStrengthLiveResult({}, _neutral_notes(f"estimate:{type(exc).__name__}"))

    teams_with_sample = sum(1 for e in estimates.values() if e.sample_size > 0)
    if teams_with_sample == 0:
        return TeamStrengthLiveResult(
            {},
            _neutral_notes("no_prior_match_history"),
        )

    multipliers: dict[int, float] = {}
    for row in active:
        home_id = int(row.home_team_id)
        away_id = int(row.away_team_id)
        home_est = estimates.get(home_id) or engine.estimate(
            home_id, cutoff, TS_METHOD, TS_WINDOW, TS_DECAY
        )
        away_est = estimates.get(away_id) or engine.estimate(
            away_id, cutoff, TS_METHOD, TS_WINDOW, TS_DECAY
        )
        try:
            fp = engine.fixture_probability(int(row.id), cutoff, home_est, away_est)
        except Exception:  # noqa: BLE001
            continue
        home_mult = _clamp(fp.expected_home_goals / TS_LEAGUE_AVG_GOALS)
        away_mult = _clamp(fp.expected_away_goals / TS_LEAGUE_AVG_GOALS)
        multipliers[home_id] = _merge_mult(multipliers.get(home_id), home_mult)
        multipliers[away_id] = _merge_mult(multipliers.get(away_id), away_mult)

    notes: dict[str, Any] = {
        "applied": bool(multipliers),
        "status": "active" if multipliers else "unavailable",
        "reason": None if multipliers else "no_multipliers_built",
        "method": TS_METHOD,
        "window": TS_WINDOW,
        "decay": TS_DECAY,
        "model_name": TS_MODEL_NAME,
        "model_version": TS_MODEL_VERSION,
        "feature_version": TS_FEATURE_VERSION,
        "teams_estimated": teams_with_sample,
        "fixtures_used": len(active),
        "teams_adjusted": len(multipliers),
        "cutoff_time": cutoff.isoformat(),
        "holdout": "2025-26 locked — HOLDOUT-APPROVED",
    }
    return TeamStrengthLiveResult(multipliers, notes)


def _clamp(value: float) -> float:
    return max(TS_MULT_MIN, min(TS_MULT_MAX, float(value)))


def _merge_mult(existing: float | None, new: float) -> float:
    if existing is None:
        return new
    return _clamp((existing + new) / 2.0)


def apply_multipliers_to_points(
    points: dict[int, float],
    team_by_player: dict[int, int],
    multipliers: dict[int, float],
) -> dict[int, float]:
    """Scale player xPTS by their team's EWMA fixture multiplier."""
    if not multipliers:
        return dict(points)
    out: dict[int, float] = {}
    for pid, xp in points.items():
        team_id = team_by_player.get(int(pid))
        mult = multipliers.get(int(team_id), TS_BLANK_MULT) if team_id is not None else TS_BLANK_MULT
        out[int(pid)] = round(float(xp) * float(mult), 6)
    return out


def player_team_map_from_catalog(catalog: dict[int, dict[str, Any]]) -> dict[int, int]:
    """Map player ids to team ids from the catalog."""
    mapping: dict[int, int] = {}
    for pid, row in catalog.items():
        team = row.get("team")
        if team is None:
            continue
        try:
            mapping[int(pid)] = int(team)
        except (TypeError, ValueError):
            continue
    return mapping


def ensure_registry_entry(db: Session) -> bool:
    """Idempotently register the holdout-approved EWMA model as active.

    Returns True when an active entry is present after the call. Failures are
    non-fatal — registry is bookkeeping, not required for live scoring.
    """
    try:
        from fpl_intelligence.prediction.models import ModelRegistryEntry
        from sqlalchemy import select
    except Exception as exc:  # noqa: BLE001
        logger.warning("team strength registry import failed: %s", exc)
        return False

    try:
        existing = db.execute(
            select(ModelRegistryEntry).where(
                ModelRegistryEntry.model_name == TS_MODEL_NAME,
                ModelRegistryEntry.model_version == TS_MODEL_VERSION,
            )
        ).scalar_one_or_none()
    except Exception as exp:
        logger.warning("team strength registry lookup failed: %s", exp)
        return False

    if existing is not None:
        if existing.status != "active":
            try:
                actives = db.execute(
                    select(ModelRegistryEntry).where(
                        ModelRegistryEntry.model_name == TS_MODEL_NAME,
                        ModelRegistryEntry.status == "active",
                    )
                ).scalars().all()
                for row in actives:
                    row.status = "staged"
                existing.status = "active"
                db.commit()
            except Exception as exp:
                logger.warning("team strength registry promote failed: %s", exp)
                try:
                    db.rollback()
                except Exception:
                    pass
                return False
        return True

    try:
        actives = db.execute(
            select(ModelRegistryEntry).where(
                ModelRegistryEntry.model_name == TS_MODEL_NAME,
                ModelRegistryEntry.status == "active",
            )
        ).scalars().all()
        for row in actives:
            row.status = "staged"

        entry = ModelRegistryEntry(
            model_name=TS_MODEL_NAME,
            model_version=TS_MODEL_VERSION,
            model_type="team_strength_ewma",
            feature_version=TS_FEATURE_VERSION,
            hyperparameters={
                "method": TS_METHOD,
                "window": TS_WINDOW,
                "decay": TS_DECAY,
                "league_average_goals": TS_LEAGUE_AVG_GOALS,
            },
            metrics={
                "holdout_season": "2025-26",
                "holdout_status": "HOLDOUT-APPROVED",
                "mae": 0.9602,
                "rmse": 1.1980,
                "multiclass_log_loss": 1.0903,
                "home_win_brier": 0.2424,
                "clean_sheet_brier": 0.2155,
            },
            artifact_location=f"builtin:{TS_MODEL_NAME}:{TS_MODEL_VERSION}",
            status="active",
        )
        db.add(entry)
        db.commit()
        return True
    except Exception as exp:
        logger.warning("team strength registry insert failed: %s", type(exp).__name__)
        try:
            db.rollback()
        except Exception:
            pass
        return False
