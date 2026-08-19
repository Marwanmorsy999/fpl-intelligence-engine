"""Squad state management service."""

from __future__ import annotations

from datetime import datetime
from threading import Lock

from fpl_intelligence.squad.models import SquadStateCreate, SquadStateResponse


class SquadService:
    """In-memory service for storing and retrieving the user's squad state.

    Thread-safe for concurrent API access. State is not persisted across
    restarts; that is a deployment-level concern.
    """

    def __init__(self) -> None:
        self._state: SquadStateResponse | None = None
        self._lock = Lock()

    def set_squad(self, payload: SquadStateCreate) -> SquadStateResponse:
        """Persist a new squad state and return the stored representation."""
        with self._lock:
            self._state = SquadStateResponse(
                **payload.model_dump(),
                updated_at=datetime.utcnow(),
            )
            return self._state

    def get_squad(self) -> SquadStateResponse | None:
        """Return the current squad state, or None if not set."""
        with self._lock:
            return self._state

    def clear(self) -> None:
        """Remove the stored squad state (test helper)."""
        with self._lock:
            self._state = None
