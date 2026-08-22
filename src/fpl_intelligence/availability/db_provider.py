"""Phase 7 DB-backed availability provider implementation.

Implements :class:`AvailabilityProvider` by querying the database for
corroborated :class:`AvailabilityEvent` records. Also implements
:class:`NewsSource` and :class:`NewsProvider` as DB-stored concrete
providers so the pipeline can operate on persisted data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.models import (
    AvailabilityArticle,
    AvailabilityEvent,
    AvailabilitySource,
    AvailabilityStatus,
    TrainingReport,
)
from fpl_intelligence.availability.providers import (
    AvailabilityProvider,
    NewsProvider,
    NewsSource,
)


class DBAvailabilityProvider(AvailabilityProvider):
    """Availability provider backed by the availability_events table.

    Events are produced by the :class:`EvidenceCorroborator` and stored
    immutably. This provider queries them at evaluation time.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_availability(
        self, player_id: int, game_time: datetime
    ) -> tuple[AvailabilityStatus, float, list[str]]:
        result = self.get_availability_batch([player_id], game_time)
        entry = result.get(player_id)
        if entry is None:
            return AvailabilityStatus.UNKNOWN, 0.0, []
        return entry

    def get_availability_batch(
        self, player_ids: list[int], game_time: datetime
    ) -> dict[int, tuple[AvailabilityStatus, float, list[str]]]:
        results: dict[int, tuple[AvailabilityStatus, float, list[str]]] = {}
        for pid in player_ids:
            event = self.db.scalar(
                select(AvailabilityEvent)
                .where(
                    AvailabilityEvent.player_id == pid,
                    AvailabilityEvent.is_current.is_(True),
                    AvailabilityEvent.valid_from <= game_time,
                )
                .order_by(
                    AvailabilityEvent.valid_from.desc(),
                    AvailabilityEvent.confidence.desc(),
                )
            )
            if event is None:
                results[pid] = (AvailabilityStatus.UNKNOWN, 0.0, [])
            else:
                # Retrieve source names via the event's primary source.
                sources = self._sources_for_event(event.id)
                results[pid] = (
                    AvailabilityStatus(event.status),
                    event.confidence,
                    sources,
                )
        return results

    def is_training_limited(self, player_id: int, cutoff: datetime) -> tuple[bool, float | None]:
        report = self.db.scalar(
            select(TrainingReport)
            .where(
                TrainingReport.player_id == player_id,
                TrainingReport.session_at <= cutoff,
            )
            .order_by(TrainingReport.session_at.desc())
        )
        if report is None:
            return False, None
        return report.limited, report.training_load

    def _sources_for_event(self, event_id: int) -> list[str]:
        """Resolve source names for an availability event.

        The schema models a single, direct provenance link from an event to its
        primary source via ``AvailabilityEvent.primary_source_id``. There is NO
        event-to-evidence foreign key (evidence links to articles, not events),
        so multiple-source provenance cannot be reconstructed from the persisted
        schema alone.

        This method therefore:
        1. Queries the event's ``primary_source_id`` and returns its source name.
        2. Returns ``[]`` when the event has no primary source (this is an
           explicit "no source" state, not a fabricated empty list).

        If multi-source provenance is ever required, it must be added with a
        real event<->evidence association in a future migration rather than
        estimated here.
        """
        event = self.db.get(AvailabilityEvent, event_id)
        if event is None or event.primary_source_id is None:
            return []
        source = self.db.get(AvailabilitySource, event.primary_source_id)
        if source is None:
            return []
        return [source.name]


class DBNewsProvider(NewsProvider):
    """News provider that returns articles already persisted in the DB.

    This allows the pipeline to work with pre-ingested data (for testing
    and for replay scenarios) while the NewsSource abstraction supports
    live fetching in production.
    """

    def __init__(self, db: Session):
        self._db = db

    def get_sources(self) -> list[NewsSource]:
        rows = self._db.execute(select(AvailabilitySource)).scalars().all()
        return [DBNewsSource(s) for s in rows]

    def fetch_evidence(self, since: datetime | None = None) -> list[dict[str, Any]]:
        query = select(AvailabilityArticle)
        if since is not None:
            query = query.where(AvailabilityArticle.published_at >= since)
        articles = self._db.execute(query).scalars().all()
        return [
            {
                "url": a.url,
                "headline": a.headline,
                "published_at": a.published_at,
                "content": a.content,
                "source_id": a.source_id,
                "source_name": a.source.name if a.source else None,
            }
            for a in articles
        ]


class DBNewsSource(NewsSource):
    """A single DB-backed news source."""

    def __init__(self, source: AvailabilitySource):
        self._source = source

    @property
    def source_name(self) -> str:
        return self._source.name

    @property
    def reliability(self) -> str:
        return self._source.reliability

    def fetch_articles(self, since: datetime | None = None) -> list[dict[str, Any]]:
        return []
