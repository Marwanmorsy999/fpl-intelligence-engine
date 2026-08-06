"""Temporal classification for historical availability events (Phase 7.2).

The critical no-look-ahead distinction is preserved: EVENT TIME (when the event
occurred) is NOT the same as INFORMATION AVAILABILITY TIME (when the information
was available to the system / decision-maker).

For strict backtesting, an event is eligible only when there is sufficient
evidence that the information was available before the historical decision
cutoff (gameweek deadline). Events that merely occurred before the deadline but
whose publication/availability timing cannot be established are classified
HISTORICAL_EVENT_ONLY and are NOT used as strict pre-deadline intelligence.

Timestamps preserved per event:
- event_time: when the football/availability event happened.
- published_at: when the source published the information.
- available_at: earliest time we can legitimately claim access.
- ingested_at: when our pipeline actually collected it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fpl_intelligence.availability.models import TemporalClass


@dataclass
class AvailabilityTimestamps:
    """All temporal markers for a single availability event."""

    event_time: datetime | None = None
    published_at: datetime | None = None
    available_at: datetime | None = None
    ingested_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_time": self.event_time.isoformat() if self.event_time else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "ingested_at": self.ingested_at.isoformat() if self.ingested_at else None,
        }


def classify_temporal(
    timestamps: AvailabilityTimestamps,
    *,
    strict_backtest_safe: bool = True,
) -> str:
    """Classify a historical availability event into a TemporalClass.

    Strict mode requires sufficient temporal evidence that the information was
    available before the decision cutoff. Without a publication/availability
    timestamp we cannot establish strict pre-deadline availability, so the event
    is classified HISTORICAL_EVENT_ONLY (or, if it is purely an outcome, we let
    the caller pass OUTCOME_ONLY via the timestamps/context).

    Rules:
    - If ``strict_backtest_safe`` is False, nothing is strict: events carrying a
      publication/availability timestamp are HISTORICAL_EVENT_ONLY (we do not
      auto-claim strictness), and events with no usable timestamp are UNKNOWN.
    - If strict mode is on:
        * published_at or available_at present  -> STRICT_BACKTEST_SAFE *only if*
          that timestamp is well-defined. (The vs-deadline comparison is done
          separately by ``is_event_eligible_before_cutoff``.)
        * event_time only (no publication/avail) -> HISTORICAL_EVENT_ONLY.
        * no timestamps at all                   -> UNKNOWN.
    """
    if not strict_backtest_safe:
        if timestamps.published_at is not None or timestamps.available_at is not None:
            return TemporalClass.HISTORICAL_EVENT_ONLY
        return TemporalClass.UNKNOWN

    # Strict mode.
    if timestamps.published_at is not None or timestamps.available_at is not None:
        return TemporalClass.STRICT_BACKTEST_SAFE
    if timestamps.event_time is not None:
        return TemporalClass.HISTORICAL_EVENT_ONLY
    return TemporalClass.UNKNOWN


def is_event_eligible_before_cutoff(
    timestamps: AvailabilityTimestamps,
    cutoff: datetime | None,
) -> bool:
    """Return True if the event's information was available before `cutoff`.

    Uses the earliest of published_at / available_at as the information
    availability time. If no such timestamp exists, or no cutoff is provided,
    returns False (never silently treats an event as strict pre-deadline).
    """
    if cutoff is None:
        return False
    info_time = timestamps.published_at or timestamps.available_at
    if info_time is None:
        return False
    return info_time <= cutoff


def classify_and_check_eligibility(
    timestamps: AvailabilityTimestamps,
    cutoff: datetime | None,
    *,
    strict_backtest_safe: bool = True,
) -> tuple[str, bool]:
    """Classify an event and check strict pre-deadline eligibility together.

    Returns ``(temporal_class, eligible_before_cutoff)``. An event is only both
    STRICT_BACKTEST_SAFE *and* eligible when its information was available before
    the cutoff. This is the single source of truth for the strict path.
    """
    temporal_class = classify_temporal(timestamps, strict_backtest_safe=strict_backtest_safe)
    eligible = (
        temporal_class == TemporalClass.STRICT_BACKTEST_SAFE
        and is_event_eligible_before_cutoff(timestamps, cutoff)
    )
    return temporal_class, eligible


@dataclass
class TemporalClassificationResult:
    """Result of classifying a batch of historical events."""

    strict_safe: int = 0
    historical_event_only: int = 0
    outcome_only: int = 0
    unknown: int = 0
    eligible_before_cutoff: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strict_safe": self.strict_safe,
            "historical_event_only": self.historical_event_only,
            "outcome_only": self.outcome_only,
            "unknown": self.unknown,
            "eligible_before_cutoff": self.eligible_before_cutoff,
            "details": self.details,
        }
