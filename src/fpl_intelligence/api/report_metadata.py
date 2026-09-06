"""Phase 5 — decision report metadata.

Every decision response carries a small, structured metadata block so
the UI can show:

* when the report was generated
* which model version produced it
* which data snapshot it was built against
* the current refresh state (``fresh | refreshing | stale | unavailable``)
* the next-allowed manual refresh timestamp

The metadata is intentionally **non-personal**: it contains no
session id, no entry id, no per-user fields. The UI layer is
responsible for adding any user-facing labels.

This module is a pure factory — no DB, no I/O. The store layer
populates these values when it constructs a snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class ReportMetadata:
    """Structured metadata for one decision report response."""

    model_version: str
    data_snapshot_id: str
    generated_at: str  # ISO-8601 UTC
    refresh_state: str  # fresh | refreshing | stale | unavailable
    refresh_window_seconds: float
    next_manual_refresh_allowed_at: str  # ISO-8601 UTC

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_metadata(
    *,
    model_version: str,
    data_snapshot_id: str,
    generated_at: datetime,
    refresh_state: str,
    refresh_window_seconds: float,
) -> ReportMetadata:
    """Build a :class:`ReportMetadata` with computed refresh-gate time."""
    next_allowed = generated_at.astimezone(UTC)
    from datetime import timedelta

    next_allowed = next_allowed + timedelta(seconds=float(refresh_window_seconds))
    return ReportMetadata(
        model_version=str(model_version),
        data_snapshot_id=str(data_snapshot_id),
        generated_at=generated_at.astimezone(UTC).isoformat(),
        refresh_state=str(refresh_state),
        refresh_window_seconds=float(refresh_window_seconds),
        next_manual_refresh_allowed_at=next_allowed.isoformat(),
    )
