"""Phase 7 provider abstractions.

NEWS / AVAILABILITY → EVIDENCE → EVENT → CONFIDENCE → AVAILABILITY STATE
→ MINUTES MODEL → PLAYER PREDICTION → DISTRIBUTION → DECISION ENGINE

These ABCs decouple data acquisition (news, training reports, press conferences)
from evidence corroboration and state derivation, maintaining the same
abstraction discipline as the prediction and optimization layers.
"""
from __future__ import annotations

import abc
from datetime import datetime
from typing import Any

from fpl_intelligence.availability.models import (
    AvailabilityStatus,
)


class NewsSource(abc.ABC):
    """Abstract base for a single news/availability data source.

    Implementations fetch raw content (articles, transcripts, structured APIs)
    and yield :class:`RawEvidence` items. The source carries a reliability
    tier used by the corroboration engine.
    """

    @property
    @abc.abstractmethod
    def source_name(self) -> str:
        """Human-readable name of this source."""

    @property
    @abc.abstractmethod
    def reliability(self) -> str:
        """One of the SourceReliability tiers."""

    @abc.abstractmethod
    def fetch_articles(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """Return list of raw article dicts with keys url, headline,
        published_at, content (HTML/text), team_id (optional)."""


class NewsProvider(abc.ABC):
    """Aggregator that fetches from multiple NewsSources.

    The concrete implementation composes registered :class:`NewsSource`
    instances and yields raw evidence for the corroboration engine.
    """

    @abc.abstractmethod
    def get_sources(self) -> list[NewsSource]:
        """Return all registered sources."""

    @abc.abstractmethod
    def fetch_evidence(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """Fetch new articles from all sources and return them as raw dicts."""


class RawEvidence:
    """A single piece of availability evidence extracted from a source.

    This is the bridge between raw article content and the structured
    :class:`AvailabilityEvidence` DB model.
    """

    def __init__(
        self,
        player_id: int,
        source_name: str,
        reliability: str,
        evidence_type: str,
        status_mentioned: str,
        description: str | None,
        published_at: datetime | None,
        valid_from: datetime | None,
        valid_to: datetime | None | None,
        raw_data: dict[str, Any] | None = None,
    ):
        self.player_id = player_id
        self.source_name = source_name
        self.reliability = reliability
        self.evidence_type = evidence_type
        self.status_mentioned = status_mentioned
        self.description = description
        self.published_at = published_at
        self.valid_from = valid_from
        self.valid_to = valid_to
        self.raw_data = raw_data or {}


class AvailabilityProvider(abc.ABC):
    """Abstract provider for current availability state queries.

    The concrete implementation aggregates :class:`AvailabilityEvent` records
    from the DB (produced by the corroboration engine) and returns a
    per-player availability state at a given cutoff time.

    This is the interface the prediction layer consults when refreshing
    player predictions with availability information.
    """

    @abc.abstractmethod
    def get_availability(
        self, player_id: int, game_time: datetime
    ) -> tuple[AvailabilityStatus, float, list[str]]:
        """Return (status, confidence, source_names) for a player at game_time."""

    @abc.abstractmethod
    def get_availability_batch(
        self, player_ids: list[int], game_time: datetime
    ) -> dict[int, tuple[AvailabilityStatus, float, list[str]]]:
        """Batch version of get_availability."""

    @abc.abstractmethod
    def is_training_limited(
        self, player_id: int, cutoff: datetime
    ) -> tuple[bool, float | None]:
        """Return (is_limited, training_load) based on latest training report."""
