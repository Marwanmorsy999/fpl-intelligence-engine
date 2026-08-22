"""Phase 11.1 — Structured fact models for the API-first integration.

These dataclasses are the lingua franca between the three external connectors
(official FPL, API-Football, football-data.org) and the quantitative decision
engine. Connectors emit :class:`PlayerFact` (raw, source-specific structured
data normalised into a common shape); the :class:`LiveFactInjector` converts
those into :class:`FactOverride` objects that the decision layer can apply on
top of the baseline quantitative predictions.

Nothing here imports the Phase 1–8 model internals: a :class:`FactOverride` is
pure data the :class:`~fpl_intelligence.data_providers.decision_bridge.FactOverrideProvider`
consumes without mutating any upstream model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class FactSource(StrEnum):
    """Which external API produced a fact."""

    FPL_OFFICIAL = "fpl_official"
    API_FOOTBALL = "api_football"
    FOOTBALL_DATA_ORG = "football_data_org"
    UNKNOWN = "unknown"


class FactConfidence(StrEnum):
    """How much weight a fact override should carry."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class PlayerFact:
    """One normalised structured fact about a player, from one source.

    Source-specific identifiers are preserved (``fpl_player_id``,
    ``api_football_player_id``) so the injector can key overrides on the FPL
    player id after entity resolution. Connectors that cannot resolve an FPL id
    leave ``fpl_player_id`` as ``None`` and the fact is skipped by the injector
    unless a mapping is supplied (entity resolution is owned by Phase 9.2.1 and
    is out of scope here).
    """

    source: FactSource
    name: str
    fpl_player_id: int | None = None
    api_football_player_id: int | None = None
    team_id: int | None = None
    team_name: str | None = None
    #: Normalised availability status string (e.g. "available", "injured",
    #: "suspended", "doubtful", "out", "bench", "start").
    status: str | None = None
    #: FPL ``chance_of_playing_*`` value, 0-100, or ``None``.
    chance_of_playing: int | None = None
    news: str | None = None
    price: float | None = None
    expected_minutes: float | None = None
    is_starting: bool = False
    is_bench: bool = False
    is_injured: bool = False
    fixture_difficulty: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "name": self.name,
            "fpl_player_id": self.fpl_player_id,
            "api_football_player_id": self.api_football_player_id,
            "team_id": self.team_id,
            "team_name": self.team_name,
            "status": self.status,
            "chance_of_playing": self.chance_of_playing,
            "news": self.news,
            "price": self.price,
            "expected_minutes": self.expected_minutes,
            "is_starting": self.is_starting,
            "is_bench": self.is_bench,
            "is_injured": self.is_injured,
            "fixture_difficulty": self.fixture_difficulty,
        }


@dataclass
class FactOverride:
    """A hard fact that overrides a baseline quantitative prediction.

    Produced by :class:`LiveFactInjector`. Only the fields that are actually
    overridden are non-``None``; the consuming :class:`FactOverrideProvider`
    leaves any ``None`` field at its baseline value.
    """

    player_id: int
    source: FactSource
    start_probability: float | None = None
    expected_minutes: float | None = None
    availability_status: str | None = None
    reason: str = ""
    confidence: FactConfidence = FactConfidence.HIGH
    fetched_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "source": self.source.value,
            "start_probability": self.start_probability,
            "expected_minutes": self.expected_minutes,
            "availability_status": self.availability_status,
            "reason": self.reason,
            "confidence": self.confidence.value,
        }
