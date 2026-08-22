"""Normalization layer for historical availability events (Phase 7.2).

Converts provider-specific raw availability events into a canonical dict shape
consumed by the importer. Provider-specific schemas are mapped to a single
provider-independent representation that carries:

- player / team / season / gameweek references
- canonical event type + status
- all temporal markers (event_time, published_at, available_at, ingested_at)
- source + reliability
- provider provenance (provider, provider_event_id)
"""

from __future__ import annotations

from typing import Any

from fpl_intelligence.availability.historical.event_types import (
    event_type_to_evidence,
    event_type_to_status,
    parse_event_type,
)
from fpl_intelligence.availability.historical.temporal import AvailabilityTimestamps


def normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a provider raw availability event into a canonical dict.

    The raw event may come from any adapter. It must contain at minimum a
    ``player_id`` and either ``event_type`` or enough free text to derive one.
    All output fields are plain JSON-safe values (timestamps are ISO strings)
    so the result can be persisted and audited consistently.
    """
    timestamps: AvailabilityTimestamps | None = raw.get("timestamps")
    if timestamps is None:
        timestamps = AvailabilityTimestamps(
            event_time=raw.get("event_time"),
            published_at=raw.get("published_at"),
            available_at=raw.get("available_at"),
            ingested_at=raw.get("ingested_at"),
        )

    event_type = raw.get("event_type")
    if not event_type:
        event_type = parse_event_type([str(raw.get("status", ""))])

    # The canonical status MUST come from the canonical event-type mapping, not
    # from the provider's raw status string verbatim. Raw status strings (e.g.
    # "injury", "suspension", "training_limited") are event-type-like labels
    # that do not necessarily match the AvailabilityStatus enum; passing them
    # through would violate the enum constraint on AvailabilityEvent.status.
    status = event_type_to_status(event_type)

    return {
        "provider": raw.get("provider", ""),
        "provider_event_id": raw.get("provider_event_id", ""),
        "season_code": raw.get("season_code"),
        "player_id": raw.get("player_id"),
        "team_id": raw.get("team_id"),
        "event_type": event_type,
        "status": status,
        "evidence_type": event_type_to_evidence(event_type),
        "description": raw.get("description"),
        "confidence": raw.get("confidence", 0.5),
        "source_name": raw.get("source_name", ""),
        "reliability": raw.get("reliability", "unverified"),
        "environment": raw.get("environment", "real"),
        "temporal": timestamps.to_dict(),
    }
