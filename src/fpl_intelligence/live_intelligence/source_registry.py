"""Phase 9.2 — Source Registry.

A *configuration model* for the multi-source ingestion foundation. It declares
what kinds of unstructured sources the engine will ever accept and, critically,
assigns each one an explicit **reliability tier** so that downstream consumers
(analyst guardrails, weighting, validation-evidence selection) can reason about
a source's trustworthiness without re-deriving it at every call site.

This module deliberately does NOT modify the quantitative Phases 1–8 stack, nor
the Phase 9.1 extraction engine. It is a thin, additive layer that:

* enumerates the eight accepted :class:`SourceType` values,
* enumerates the five :class:`ReliabilityTier` values (TIER_0 … TIER_4),
* classifies a tier from a source type (plus optional official-club override),
* and bridges onto the existing Phase 9.1 ``live_intelligence_sources`` table so
  that a registered source is real, auditable, and reachable by the ledger.

The tiers are intentionally coarse and ordered. A higher tier number means a
less reliable source; TIER_0 (official structured API) is the most trustworthy
and TIER_4 (social, unverified) the least. Everything else in the pipeline
consumes the tier, never the raw source string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.models import SourceReliability
from fpl_intelligence.domain.environment import DataEnvironment
from fpl_intelligence.live_intelligence.models import (
    LiveIntelligenceSource,
    LiveSourceType,
)


class SourceType(StrEnum):
    """The eight kinds of unstructured source feeding the accumulator."""

    OFFICIAL_API = "official_api"
    PRESS_CONFERENCE = "press_conference"
    CLUB_SITE = "club_site"
    RSS = "rss"
    JOURNALIST = "journalist"
    AGGREGATOR = "aggregator"
    SOCIAL = "social"
    MANUAL = "manual"


class ReliabilityTier(StrEnum):
    """Ordered reliability tiers for ingested sources.

    A lower number is *more* reliable. The ordering is the whole point: it gives
    every downstream consumer a single, comparable axis of trust, independent of
    the free-text source label.
    """

    TIER_0_OFFICIAL_STRUCTURED = "tier_0_official_structured"
    TIER_1_OFFICIAL_UNSTRUCTURED = "tier_1_official_unstructured"
    TIER_2_RELIABLE_JOURNALIST = "tier_2_reliable_journalist"
    TIER_3_AGGREGATOR = "tier_3_aggregator"
    TIER_4_SOCIAL_UNVERIFIED = "tier_4_social_unverified"

    @property
    def rank(self) -> int:
        """Lower is more reliable. Used for sorting / comparison."""
        return {
            ReliabilityTier.TIER_0_OFFICIAL_STRUCTURED: 0,
            ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED: 1,
            ReliabilityTier.TIER_2_RELIABLE_JOURNALIST: 2,
            ReliabilityTier.TIER_3_AGGREGATOR: 3,
            ReliabilityTier.TIER_4_SOCIAL_UNVERIFIED: 4,
        }[self]

    @property
    def is_official(self) -> bool:
        """True for the two official tiers (structured and unstructured)."""
        return self in (
            ReliabilityTier.TIER_0_OFFICIAL_STRUCTURED,
            ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED,
        )

    @property
    def is_structured(self) -> bool:
        """True only for machine-readable official feeds (TIER_0)."""
        return self is ReliabilityTier.TIER_0_OFFICIAL_STRUCTURED

    @property
    def is_unverified(self) -> bool:
        """True for the lowest tier (social / unverified)."""
        return self is ReliabilityTier.TIER_4_SOCIAL_UNVERIFIED

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ReliabilityTier):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, ReliabilityTier):
            return NotImplemented
        return self.rank <= other.rank


#: Canonical tier for each source type. A manual paste is unverified by default;
#: its trust depends entirely on what was pasted, which the registrar can
#: override at registration time.
DEFAULT_TIER_BY_SOURCE_TYPE: dict[SourceType, ReliabilityTier] = {
    SourceType.OFFICIAL_API: ReliabilityTier.TIER_0_OFFICIAL_STRUCTURED,
    SourceType.PRESS_CONFERENCE: ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED,
    SourceType.CLUB_SITE: ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED,
    SourceType.RSS: ReliabilityTier.TIER_3_AGGREGATOR,
    SourceType.AGGREGATOR: ReliabilityTier.TIER_3_AGGREGATOR,
    SourceType.JOURNALIST: ReliabilityTier.TIER_2_RELIABLE_JOURNALIST,
    SourceType.SOCIAL: ReliabilityTier.TIER_4_SOCIAL_UNVERIFIED,
    SourceType.MANUAL: ReliabilityTier.TIER_4_SOCIAL_UNVERIFIED,
}

#: Worked examples, used by docs and tests, mapping a concrete source id to the
#: tier it should receive under the canonical classification.
KNOWN_SOURCE_PRESETS: dict[str, SourceType] = {
    "fpl_official_api": SourceType.OFFICIAL_API,
    "press_conference_manual": SourceType.PRESS_CONFERENCE,
    "club_site_manual": SourceType.CLUB_SITE,
    "journalist_manual": SourceType.JOURNALIST,
    "news_rss": SourceType.RSS,
    "aggregator_rss": SourceType.AGGREGATOR,
    "social_post": SourceType.SOCIAL,
    "manual_paste": SourceType.MANUAL,
}


@dataclass(frozen=True)
class SourceDefinition:
    """An immutable, auditable description of one registered source."""

    source_id: str
    source_type: SourceType
    reliability_tier: ReliabilityTier
    url: str | None = None
    is_official: bool = False
    notes: str = ""

    @property
    def rank(self) -> int:
        return self.reliability_tier.rank


def map_source_type_to_live(source_type: SourceType) -> LiveSourceType:
    """Map a Phase 9.2 :class:`SourceType` onto the Phase 9.1 DB enum."""
    return {
        SourceType.OFFICIAL_API: LiveSourceType.FPL_OFFICIAL,
        SourceType.PRESS_CONFERENCE: LiveSourceType.PRESS_CONFERENCE,
        SourceType.CLUB_SITE: LiveSourceType.CLUB_OFFICIAL,
        SourceType.RSS: LiveSourceType.NEWS_ARTICLE,
        SourceType.JOURNALIST: LiveSourceType.JOURNALIST,
        SourceType.AGGREGATOR: LiveSourceType.AGGREGATOR,
        SourceType.SOCIAL: LiveSourceType.SOCIAL_POST,
        SourceType.MANUAL: LiveSourceType.OTHER,
    }[source_type]


def map_tier_to_reliability(tier: ReliabilityTier) -> SourceReliability:
    """Map a Phase 9.2 :class:`ReliabilityTier` onto the Phase 7 DB enum.

    The Phase 7 enum is coarser than the five tiers; the mapping is the honest
    best-fit. The full tier is preserved separately on the source row so it is
    never lost, only *projected* into the existing column.
    """
    return {
        ReliabilityTier.TIER_0_OFFICIAL_STRUCTURED: SourceReliability.OFFICIAL,
        ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED: SourceReliability.OFFICIAL,
        ReliabilityTier.TIER_2_RELIABLE_JOURNALIST: SourceReliability.VERIFIED_JOURNALIST,
        ReliabilityTier.TIER_3_AGGREGATOR: SourceReliability.RELIABLE_JOURNALIST,
        ReliabilityTier.TIER_4_SOCIAL_UNVERIFIED: SourceReliability.UNVERIFIED,
    }[tier]


def _tier_marker(tier: ReliabilityTier) -> str:
    """Structured marker stored in the source ``notes`` for round-tripping."""
    return json.dumps({"phase9_2": {"tier": tier.value}})


def _parse_tier_marker(notes: str | None) -> ReliabilityTier | None:
    if not notes:
        return None
    try:
        payload = json.loads(notes)
    except (json.JSONDecodeError, ValueError):
        return None
    marker = payload.get("phase9_2")
    if not isinstance(marker, dict):
        return None
    tier_value = marker.get("tier")
    if tier_value is None:
        return None
    try:
        return ReliabilityTier(tier_value)
    except ValueError:
        return None


class SourceRegistry:
    """In-memory configuration model of accepted sources and their tiers.

    The registry is the single place that knows "a press conference is TIER_1".
    It can also project its definitions onto the Phase 9.1
    ``live_intelligence_sources`` table so that a registered source is real,
    named and reachable by the ledger. Re-registering an existing name returns
    the stored definition unchanged — source metadata is an auditable
    declaration, not a per-call parameter.

    Args:
        default_environment: The :class:`DataEnvironment` applied to sources
            created through :meth:`ensure_source` without an explicit override.
            Defaults to ``REAL``: a registered source is, by declaration, a real
            world feed rather than an engineering fixture.
    """

    def __init__(
        self,
        *,
        default_environment: DataEnvironment = DataEnvironment.REAL,
    ) -> None:
        self._sources: dict[str, SourceDefinition] = {}
        self._default_environment = default_environment

    # -- classification ----------------------------------------------------

    def classify_tier(
        self,
        source_type: SourceType,
        *,
        is_official_club: bool = False,
    ) -> ReliabilityTier:
        """Return the canonical reliability tier for a source type.

        An official club site is promoted to TIER_1 (official unstructured) even
        if it arrived through an otherwise-ambiguous channel, because a club
        speaking for itself is an official voice.
        """
        tier = DEFAULT_TIER_BY_SOURCE_TYPE[source_type]
        if is_official_club and tier < ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED:
            # Already official-structured or better; do not downgrade.
            return tier
        if is_official_club and tier > ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED:
            return ReliabilityTier.TIER_1_OFFICIAL_UNSTRUCTURED
        return tier

    # -- registration (in-memory) ------------------------------------------

    def register(
        self,
        source_id: str,
        source_type: SourceType,
        *,
        reliability_tier: ReliabilityTier | None = None,
        url: str | None = None,
        is_official: bool | None = None,
        notes: str = "",
    ) -> SourceDefinition:
        """Register (or update) a source definition in the in-memory registry.

        Re-registering an existing id overwrites the stored definition: unlike
        the database row, the registry is a working configuration that an
        operator may deliberately revise between runs.
        """
        tier = reliability_tier or self.classify_tier(source_type)
        official = is_official if is_official is not None else tier.is_official
        definition = SourceDefinition(
            source_id=source_id,
            source_type=source_type,
            reliability_tier=tier,
            url=url,
            is_official=official,
            notes=notes,
        )
        self._sources[source_id] = definition
        return definition

    def get(self, source_id: str) -> SourceDefinition | None:
        return self._sources.get(source_id)

    def tier_for(self, source_id: str) -> ReliabilityTier | None:
        definition = self._sources.get(source_id)
        return definition.reliability_tier if definition is not None else None

    def list_sources(self) -> list[SourceDefinition]:
        return sorted(self._sources.values(), key=lambda d: (d.rank, d.source_id))

    # -- persistence bridge (Phase 9.1 table) ------------------------------

    def ensure_source(
        self,
        db: Session,
        source_id: str,
        *,
        source_type: SourceType | None = None,
        reliability_tier: ReliabilityTier | None = None,
        url: str | None = None,
        is_official_club: bool | None = None,
        environment: DataEnvironment | None = None,
        publication_timestamp_trusted: bool = False,
    ) -> LiveIntelligenceSource:
        """Create (or return) the matching Phase 9.1 source row, idempotently.

        Resolution order for the source type: explicit argument, else a known
        preset id, else ``MANUAL``. The tier comes from the explicit argument,
        else from :meth:`classify_tier`, and is recorded both in the coarse
        ``reliability`` column *and* in the structured ``notes`` marker so it
        can be recovered exactly (see :meth:`load_from_db`).
        """
        from fpl_intelligence.live_intelligence.models import CaptureMethod

        existing = db.scalar(
            select(LiveIntelligenceSource).where(LiveIntelligenceSource.name == source_id)
        )
        if existing is not None:
            return existing

        resolved_type = source_type or KNOWN_SOURCE_PRESETS.get(source_id) or SourceType.MANUAL
        tier = reliability_tier or self.classify_tier(
            resolved_type,
            is_official_club=bool(is_official_club),
        )
        env = environment or self._default_environment
        official = is_official_club if is_official_club is not None else tier.is_official

        source = LiveIntelligenceSource(
            name=source_id,
            source_type=map_source_type_to_live(resolved_type),
            reliability=map_tier_to_reliability(tier),
            capture_method=CaptureMethod.MANUAL_PASTE,
            url=url,
            is_official_club=official,
            environment=env.value,
            publication_timestamp_trusted=publication_timestamp_trusted,
            notes=_tier_marker(tier),
        )
        db.add(source)
        db.flush()

        # Mirror into the in-memory registry so both views agree.
        self.register(
            source_id,
            resolved_type,
            reliability_tier=tier,
            url=url,
            is_official=official,
        )
        return source

    def load_from_db(self, db: Session) -> int:
        """Populate the in-memory registry from existing source rows.

        Returns the number of sources loaded. The canonical tier is recovered
        from the ``notes`` marker when present; otherwise it is inferred from
        the source type.
        """
        rows = list(db.scalars(select(LiveIntelligenceSource)).all())
        loaded = 0
        for row in rows:
            try:
                resolved_type = SourceType(row.source_type)
            except ValueError:
                resolved_type = SourceType.MANUAL
            tier = _parse_tier_marker(row.notes) or self.classify_tier(
                resolved_type, is_official_club=row.is_official_club
            )
            self.register(
                row.name,
                resolved_type,
                reliability_tier=tier,
                url=row.url,
                is_official=row.is_official_club,
            )
            loaded += 1
        return loaded
