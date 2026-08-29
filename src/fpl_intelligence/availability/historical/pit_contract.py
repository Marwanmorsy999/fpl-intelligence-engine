"""Formal invariants for point-in-time availability materialization."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

from fpl_intelligence.availability.historical.temporal import AvailabilityTimestamps


def validate_pit_events(
    events: Iterable[dict[str, Any]],
    *,
    cutoffs: dict[tuple[str, int | None], datetime],
) -> dict[str, int]:
    """Validate PIT event contracts before they can enter canonical storage."""
    seen: set[tuple[str, str, str]] = set()
    checked = 0
    eligible = 0
    for event in events:
        provider = str(event.get("provider") or "")
        season = str(event.get("season_code") or "")
        player = str(event.get("player_id") or "")
        event_id = str(event.get("provider_event_id") or "")
        if not provider or not season or not player or not event_id:
            raise ValueError("PIT event missing provider/season/player/provider_event_id")

        key = (season, player, event_id)
        if key in seen:
            raise ValueError(f"duplicate PIT provider event: {key}")
        seen.add(key)

        captured = _as_utc(event.get("snapshot_captured_at"))
        timestamps = event.get("timestamps")
        if not isinstance(timestamps, AvailabilityTimestamps):
            raise ValueError(f"PIT event {event_id} missing AvailabilityTimestamps")
        available_at = _as_utc(timestamps.available_at)
        published_at = _as_utc(timestamps.published_at)
        ingested_at = _as_utc(timestamps.ingested_at)
        if captured is None or available_at is None:
            raise ValueError(f"PIT event {event_id} missing snapshot/available timestamp")
        if published_at is None or published_at != captured:
            raise ValueError(f"PIT event {event_id} published_at must equal snapshot capture time")
        if available_at != captured:
            raise ValueError(f"PIT event {event_id} available_at must equal snapshot capture time")
        if ingested_at is not None and ingested_at < available_at:
            raise ValueError(f"PIT event {event_id} ingested_at precedes available_at")

        gw_raw = event.get("gameweek")
        gw = int(gw_raw) if gw_raw is not None else None
        cutoff = cutoffs.get((season, gw))
        if cutoff is not None:
            cutoff_utc = _as_utc(cutoff)
            if cutoff_utc is None or available_at > cutoff_utc:
                raise ValueError(f"PIT event {event_id} is post-deadline")
            eligible += 1
        checked += 1

    return {"checked": checked, "eligible_before_cutoff": eligible, "unique": len(seen)}


def validate_import_result(result: Any) -> dict[str, int]:
    """Validate importer conservation, entity resolution, and stage accounting."""
    audit = result.audit
    if not audit.check_conservation():
        raise ValueError(
            f"historical import conservation failed: fetched={audit.fetched} terminal={audit.terminal_total}"
        )

    pre_resolution = audit.fetched - audit.normalization_failed
    resolved_terminal = audit.matched + audit.ambiguous + audit.unmatched
    if resolved_terminal != pre_resolution:
        raise ValueError(
            "historical resolution accounting failed: "
            f"resolved={resolved_terminal} expected={pre_resolution}"
        )

    if audit.ambiguous or audit.unmatched:
        raise ValueError(
            f"PIT entity resolution is not complete: ambiguous={audit.ambiguous} unmatched={audit.unmatched}"
        )

    matched_terminal = (
        audit.persisted
        + audit.failed_persist
        + audit.skipped_duplicate
        + audit.skipped_temporal_invalid
    )
    if matched_terminal != audit.matched:
        raise ValueError(
            "matched-record accounting failed: "
            f"matched={audit.matched} terminal={matched_terminal}"
        )

    if result.eligible_before_cutoff > result.strict_safe:
        raise ValueError("eligible_before_cutoff cannot exceed strict-safe events")

    return {
        "fetched": audit.fetched,
        "persisted": audit.persisted,
        "matched": audit.matched,
        "ambiguous": audit.ambiguous,
        "unmatched": audit.unmatched,
        "eligible_before_cutoff": result.eligible_before_cutoff,
    }


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
