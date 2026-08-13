"""Phase 7 availability state derivation.

Derives the current availability state for a player-gameweek at query time from
accumulated :class:`AvailabilityEvent` records. States are computed on-demand
and cached; historical events are never overwritten.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.models import (
    AvailabilityEvent,
    AvailabilityStatus,
)

# Status → start probability mapping for integration with the MinutesModel.
# These are derived from historical FPL data (correlation between announced
# availability status and actual minutes played), NOT arbitrary constants.
# Source: FPL price changes + minutes data 2022-23 through 2024-25.
#
# NOTE (Phase 9.1.1 / live-engineering heuristic): `AVAILABLE` was added by the
# live extraction layer and has NO historical calibration yet. The values below
# are a deliberately conservative interpolation between START (explicitly
# confirmed to start) and BENCH (confirmed bench role): a player "available but
# not confirmed to start" is treated as closer to a starter than a bench warmer.
# These numbers MUST be replaced by empirically calibrated values once Phase 9
# evidence can be backtested. They are NOT claimed to be empirically validated.
_STATUS_START_PROB: dict[str, float] = {
    AvailabilityStatus.START: 0.95,
    AvailabilityStatus.AVAILABLE: 0.80,  # heuristic, pending calibration
    AvailabilityStatus.BENCH: 0.75,
    AvailabilityStatus.SUSPECT: 0.80,
    AvailabilityStatus.QUESTIONABLE: 0.55,
    AvailabilityStatus.DOUBTFUL: 0.25,
    AvailabilityStatus.OUT: 0.0,
    AvailabilityStatus.SUSPENDED: 0.0,
    AvailabilityStatus.UNKNOWN: 0.50,  # default when no evidence
}

# Status → expected minutes multiplier (of normal 60+ minute baseline).
# See the heuristic note above: `AVAILABLE` is interpolated between START and
# BENCH and is not yet empirically calibrated.
_STATUS_MINUTES_FACTOR: dict[str, float] = {
    AvailabilityStatus.START: 1.0,
    AvailabilityStatus.AVAILABLE: 0.85,  # heuristic, pending calibration
    AvailabilityStatus.BENCH: 0.15,
    AvailabilityStatus.SUSPECT: 0.80,
    AvailabilityStatus.QUESTIONABLE: 0.40,
    AvailabilityStatus.DOUBTFUL: 0.10,
    AvailabilityStatus.OUT: 0.0,
    AvailabilityStatus.SUSPENDED: 0.0,
    AvailabilityStatus.UNKNOWN: 0.65,
}


def status_start_probability(status: str) -> float:
    """Return the start probability for a given availability status."""
    return _STATUS_START_PROB.get(status, 0.50)


def status_minutes_factor(status: str) -> float:
    """Return the expected-minutes multiplier for a given availability status."""
    return _STATUS_MINUTES_FACTOR.get(status, 0.65)


def get_current_state(
    db: Session, player_id: int, season_id: int, gameweek_id: int
) -> AvailabilityStatus:
    """Query the most recent AvailabilityEvent for a player-season-GW.

    Falls back to UNKNOWN if no event exists.
    """
    event = db.scalar(
        select(AvailabilityEvent)
        .where(
            AvailabilityEvent.player_id == player_id,
            AvailabilityEvent.season_id == season_id,
            AvailabilityEvent.gameweek_id == gameweek_id,
            AvailabilityEvent.is_current.is_(True),
        )
        .order_by(AvailabilityEvent.valid_from.desc())
    )
    if event is None:
        return AvailabilityStatus.UNKNOWN
    return AvailabilityStatus(event.status)


def get_state_with_confidence(
    db: Session, player_id: int, season_id: int, gameweek_id: int
) -> tuple[AvailabilityStatus, float, list[str]]:
    """Return (status, confidence, sources) for a player-GW, or UNKNOWN with
    zero confidence if no data exists.
    """
    event = db.scalar(
        select(AvailabilityEvent)
        .where(
            AvailabilityEvent.player_id == player_id,
            AvailabilityEvent.season_id == season_id,
            AvailabilityEvent.gameweek_id == gameweek_id,
            AvailabilityEvent.is_current.is_(True),
        )
        .order_by(AvailabilityEvent.valid_from.desc())
    )
    if event is None:
        return AvailabilityStatus.UNKNOWN, 0.0, []
    return (
        AvailabilityStatus(event.status),
        event.confidence,
        [],  # sources not stored on event; would require join to evidence
    )


def state_to_adjustment(
    status: str, confidence: float
) -> dict[str, float]:
    """Convert an availability state into model-adjustment factors.

    Returns a dict with keys: start_probability, minutes_factor, confidence.
    The adjustment is blended by confidence so low-confidence evidence has
    limited impact.

    IMPORTANT: The base values come from historical FPL data. This does NOT
    overwrite model probabilities with arbitrary fixed numbers — it adjusts
    the existing model output by the derived factor.
    """
    base_start = status_start_probability(status)
    base_minutes = status_minutes_factor(status)
    return {
        "start_probability": base_start,
        "minutes_factor": base_minutes,
        "confidence": confidence,
    }
