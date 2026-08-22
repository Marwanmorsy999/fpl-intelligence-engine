"""Canonical historical availability event types (Phase 7.2).

Defines a rich set of canonical event types and maps them to the existing
Phase 7 canonical statuses/evidence types. The mapping is deliberately
conservative: we never infer a more specific medical/availability state than
the source supports, and we use UNKNOWN where appropriate.
"""

from __future__ import annotations

from enum import StrEnum

from fpl_intelligence.availability.models import AvailabilityStatus, EvidenceType


class HistoricalEventType(StrEnum):
    """Canonical historical availability event types."""

    INJURY = "injury"
    INJURY_UPDATE = "injury_update"
    ILLNESS = "illness"
    SUSPENSION = "suspension"
    SUSPENSION_RISK = "suspension_risk"
    TRAINING_ABSENCE = "training_absence"
    TRAINING_LIMITED = "training_limited"
    TRAINING_FULL = "training_full"
    RETURN_TO_TRAINING = "return_to_training"
    EXPECTED_RETURN = "expected_return"
    CONFIRMED_RETURN = "confirmed_return"
    EXPECTED_ABSENCE = "expected_absence"
    AVAILABLE = "available"
    DOUBTFUL = "doubtful"
    UNKNOWN = "unknown"


#: Default AvailabilityStatus for each historical event type.
#: Conservative: most event types map to OUT / DOUBTFUL / AVAILABLE / UNKNOWN.
_EVENT_TYPE_STATUS: dict[str, str] = {
    HistoricalEventType.INJURY: AvailabilityStatus.OUT,
    HistoricalEventType.INJURY_UPDATE: AvailabilityStatus.DOUBTFUL,
    HistoricalEventType.ILLNESS: AvailabilityStatus.OUT,
    HistoricalEventType.SUSPENSION: AvailabilityStatus.SUSPENDED,
    HistoricalEventType.SUSPENSION_RISK: AvailabilityStatus.SUSPECT,
    HistoricalEventType.TRAINING_ABSENCE: AvailabilityStatus.DOUBTFUL,
    HistoricalEventType.TRAINING_LIMITED: AvailabilityStatus.QUESTIONABLE,
    HistoricalEventType.TRAINING_FULL: AvailabilityStatus.START,
    HistoricalEventType.RETURN_TO_TRAINING: AvailabilityStatus.START,
    HistoricalEventType.EXPECTED_RETURN: AvailabilityStatus.SUSPECT,
    HistoricalEventType.CONFIRMED_RETURN: AvailabilityStatus.START,
    HistoricalEventType.EXPECTED_ABSENCE: AvailabilityStatus.OUT,
    #: ``AVAILABLE`` now maps to the canonical ``available`` status (Phase 9.1.1)
    #: rather than being collapsed into ``START``: a player reported available is
    #: fit and in contention but not necessarily confirmed to start.
    HistoricalEventType.AVAILABLE: AvailabilityStatus.AVAILABLE,
    HistoricalEventType.DOUBTFUL: AvailabilityStatus.DOUBTFUL,
    HistoricalEventType.UNKNOWN: AvailabilityStatus.UNKNOWN,
}

#: Default EvidenceType for each historical event type.
_EVENT_TYPE_EVIDENCE: dict[str, str] = {
    HistoricalEventType.INJURY: EvidenceType.INJURY,
    HistoricalEventType.INJURY_UPDATE: EvidenceType.RECOVERY_UPDATE,
    HistoricalEventType.ILLNESS: EvidenceType.INJURY,
    HistoricalEventType.SUSPENSION: EvidenceType.SUSPENSION,
    HistoricalEventType.SUSPENSION_RISK: EvidenceType.SUSPENSION,
    HistoricalEventType.TRAINING_ABSENCE: EvidenceType.TRAINING,
    HistoricalEventType.TRAINING_LIMITED: EvidenceType.TRAINING,
    HistoricalEventType.TRAINING_FULL: EvidenceType.TRAINING,
    HistoricalEventType.RETURN_TO_TRAINING: EvidenceType.TRAINING,
    HistoricalEventType.EXPECTED_RETURN: EvidenceType.RECOVERY_UPDATE,
    HistoricalEventType.CONFIRMED_RETURN: EvidenceType.RECOVERY_UPDATE,
    HistoricalEventType.EXPECTED_ABSENCE: EvidenceType.INJURY,
    HistoricalEventType.AVAILABLE: EvidenceType.FITNESS,
    HistoricalEventType.DOUBTFUL: EvidenceType.FITNESS,
    HistoricalEventType.UNKNOWN: EvidenceType.FITNESS,
}


def event_type_to_status(event_type: str) -> str:
    """Return the canonical AvailabilityStatus for an event type (UNKNOWN default)."""
    return _EVENT_TYPE_STATUS.get(event_type, AvailabilityStatus.UNKNOWN)


def event_type_to_evidence(event_type: str) -> str:
    """Return the canonical EvidenceType for an event type (FITNESS default)."""
    return _EVENT_TYPE_EVIDENCE.get(event_type, EvidenceType.FITNESS)


def parse_event_type(labels: list[str] | tuple[str, ...]) -> str:
    """Map a set of source labels to a single canonical event type.

    Matching is case-insensitive and substring-based. The most specific label
    wins in priority order. Returns UNKNOWN when nothing matches.
    """
    text = " ".join(labels).lower()
    for key in _EVENT_TYPE_STATUS:
        if key.lower() in text:
            return key
    return HistoricalEventType.UNKNOWN
