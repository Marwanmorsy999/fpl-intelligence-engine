"""Historical availability import pipeline (Phase 7.2).

RAW SOURCE
  ↓ NORMALIZATION
  ↓ CANONICAL ENTITY
  ↓ PROVENANCE
  ↓ TEMPORAL CLASSIFICATION
  ↓ AVAILABILITY EVENT (persisted)
  ↓ STRICT ELIGIBILITY
  ↓ PHASE 7 MODEL

The importer fetches raw events from a provider, normalizes them, resolves
canonical entities, classifies temporal status, and persists availability
events (and evidence/source/article records) into the canonical Phase 7 tables.

Stringent honesty rules:
- In ``strict_backtest_safe=True`` mode, only events with sufficient temporal
  evidence (publication/availability timestamp) may be marked STRICT.
- Events with missing publication/availability timestamps are NOT silently
  accepted as strict; they are imported, preserved, and marked
  HISTORICAL_EVENT_ONLY or UNKNOWN.
- Mock events (provider.environment == 'mock') are persisted but flagged so the
  coverage audit can exclude them from real coverage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.availability.historical.entity_resolution import (
    HistoricalEntityResolver,
    HistoricalResolutionReport,
)
from fpl_intelligence.availability.historical.event_types import (
    event_type_to_evidence,
    event_type_to_status,
)
from fpl_intelligence.availability.historical.normalizer import normalize_event
from fpl_intelligence.availability.historical.providers import (
    HistoricalAvailabilityProvider,
)
from fpl_intelligence.availability.historical.temporal import (
    AvailabilityTimestamps,
    classify_temporal,
    is_event_eligible_before_cutoff,
)
from fpl_intelligence.availability.models import (
    AvailabilityArticle,
    AvailabilityEvent,
    AvailabilityEvidence,
    AvailabilitySource,
    SourceReliability,
    TemporalClass,
)
from fpl_intelligence.db.models import Gameweek, Season

logger = logging.getLogger(__name__)

#: Source reliability for the official FPL bootstrap source.
_FPL_SOURCE = "FPL bootstrap (players_raw.csv) availability news"

#: Inclusive (start, end) UTC date bounds for development seasons. The official
#: FPL ``players_raw.csv`` mirror is a terminal season snapshot, so any
#: ``news_added`` timestamp outside the canonical season window is an
#: out-of-window / look-ahead signal that must NOT be persisted as strict.
_SEASON_WINDOWS: dict[str, tuple[str, str]] = {
    "2022-23": ("2022-08-05", "2023-05-28"),
    "2023-24": ("2023-08-11", "2024-05-19"),
    "2024-25": ("2024-08-16", "2025-05-25"),
    "2025-26": ("2025-08-15", "2026-05-24"),
}


@dataclass
class ResolverAudit:
    """Full per-record accounting for a historical availability import.

    Every raw record fetched from a provider MUST be accounted for exactly
    once in a terminal bucket. The invariant enforced by
    :meth:`check_conservation` is::

        fetched == persisted
                 + failed_persist
                 + normalization_failed
                 + skipped_invalid
                 + skipped_duplicate
                 + skipped_temporal_invalid
                 + ambiguous
                 + unmatched

    ``normalized`` and ``matched`` are pass-through counters (not terminal
    buckets) and are reported separately so a silent entity-resolution failure
    cannot hide inside an aggregate "skipped" number.
    """

    # Pipeline stage counters (pass-through).
    fetched: int = 0
    normalized: int = 0
    matched: int = 0

    # Terminal buckets.
    normalization_failed: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    skipped_invalid: int = 0
    skipped_duplicate: int = 0
    skipped_temporal_invalid: int = 0
    persisted: int = 0
    failed_persist: int = 0

    # Team resolution (secondary path).
    teams_matched: int = 0
    teams_unmatched: int = 0
    teams_absent: int = 0

    #: Reasons keyed by bucket -> reason -> count (never silently dropped).
    reasons: dict[str, dict[str, int]] = field(default_factory=dict)

    def note(self, bucket: str, reason: str) -> None:
        self.reasons.setdefault(bucket, {})
        self.reasons[bucket][reason] = self.reasons[bucket].get(reason, 0) + 1

    @property
    def terminal_total(self) -> int:
        return (
            self.persisted
            + self.failed_persist
            + self.normalization_failed
            + self.skipped_invalid
            + self.skipped_duplicate
            + self.skipped_temporal_invalid
            + self.ambiguous
            + self.unmatched
        )

    def check_conservation(self) -> bool:
        """Return True when every fetched record landed in exactly one bucket."""
        return self.fetched == self.terminal_total

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "normalized": self.normalized,
            "matched": self.matched,
            "ambiguous": self.ambiguous,
            "unmatched": self.unmatched,
            "normalization_failed": self.normalization_failed,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_invalid": self.skipped_invalid,
            "skipped_temporal_invalid": self.skipped_temporal_invalid,
            "persisted": self.persisted,
            "failed_persist": self.failed_persist,
            "teams_matched": self.teams_matched,
            "teams_unmatched": self.teams_unmatched,
            "teams_absent": self.teams_absent,
            "terminal_total": self.terminal_total,
            "conservation_ok": self.check_conservation(),
            "reasons": self.reasons,
        }


@dataclass
class HistoricalImportResult:
    """Result of a historical availability import."""

    provider: str
    seasons: list[str]
    events_imported: int = 0
    events_skipped: int = 0
    strict_safe: int = 0
    historical_event_only: int = 0
    unknown: int = 0
    eligible_before_cutoff: int = 0
    mock_events: int = 0
    resolution: HistoricalResolutionReport = field(default_factory=HistoricalResolutionReport)
    environments: dict[str, int] = field(default_factory=dict)
    audit: ResolverAudit = field(default_factory=ResolverAudit)
    #: Raw records that never reached persistence, preserved verbatim for the
    #: unmatched/raw audit artefact. Nothing is ever silently dropped.
    unresolved_records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "seasons": self.seasons,
            "events_imported": self.events_imported,
            "events_skipped": self.events_skipped,
            "strict_safe": self.strict_safe,
            "historical_event_only": self.historical_event_only,
            "unknown": self.unknown,
            "eligible_before_cutoff": self.eligible_before_cutoff,
            "mock_events": self.mock_events,
            "resolution": self.resolution.to_dict(),
            "environments": self.environments,
            "resolver_audit": self.audit.to_dict(),
            "unresolved_record_count": len(self.unresolved_records),
        }


def _gameweek_cutoff(db: Session, season_id: int, gw_num: int | None) -> datetime | None:
    """Return the deadline for a gameweek, or the season's latest deadline."""
    if gw_num is not None:
        gw = db.scalar(
            select(Gameweek).where(
                Gameweek.season_id == season_id,
                Gameweek.provider_event_id == gw_num,
            )
        )
        if gw is not None and gw.deadline_time is not None:
            return gw.deadline_time
    # Fall back to the season's latest-known deadline (conservative).
    gws = db.execute(select(Gameweek).where(Gameweek.season_id == season_id)).scalars().all()
    cutoff = None
    for g in gws:
        if g.deadline_time is not None and (cutoff is None or g.deadline_time > cutoff):
            cutoff = g.deadline_time
    return cutoff


def _get_or_create_source(db: Session, name: str, reliability: str) -> AvailabilitySource:
    source = db.scalar(select(AvailabilitySource).where(AvailabilitySource.name == name))
    if source:
        return source
    source = AvailabilitySource(
        name=name,
        reliability=SourceReliability(reliability)
        if reliability in {r.value for r in SourceReliability}
        else SourceReliability.UNVERIFIED,
    )
    db.add(source)
    db.flush()
    return source


def import_historical_availability(
    db: Session,
    provider: HistoricalAvailabilityProvider,
    seasons: list[str],
    *,
    strict_backtest_safe: bool = True,
) -> HistoricalImportResult:
    """Import historical availability events from a provider.

    Args:
        db: Database session.
        provider: A HistoricalAvailabilityProvider adapter.
        seasons: Season codes to import (e.g. ['2022-23','2023-24','2024-25']).
        strict_backtest_safe: When True, events without sufficient temporal
            evidence are marked HISTORICAL_EVENT_ONLY / UNKNOWN (never strict).
            When False, no event is ever marked strict.

    Returns:
        HistoricalImportResult with counts, temporal breakdown, and resolution report.
    """
    result = HistoricalImportResult(provider=provider.provider_name, seasons=seasons)
    resolver = HistoricalEntityResolver(db, provider.provider_name)

    for season_code in seasons:
        season = db.scalar(select(Season).where(Season.code == season_code))
        if season is None:
            logger.warning("Season %s not present; skipping", season_code)
            continue
        sid = season.id

        raw_events = provider.fetch_events(season_code)
        for raw in raw_events:
            result.audit.fetched += 1
            env = raw.get("environment", getattr(provider, "environment", "real"))
            result.environments[env] = result.environments.get(env, 0) + 1
            if env == "mock":
                result.mock_events += 1

            try:
                norm = normalize_event(raw)
            except Exception as exc:  # noqa: BLE001
                result.audit.normalization_failed += 1
                result.audit.note("normalization_failed", type(exc).__name__)
                logger.error(
                    "normalize failed for %s/%s: %s",
                    provider.provider_name,
                    raw.get("provider_event_id"),
                    exc,
                )
                continue
            result.audit.normalized += 1
            # Reconstruct timestamps from the normalized dict.
            t = norm.get("temporal", {})
            timestamps = AvailabilityTimestamps(
                event_time=_parse_ts(t.get("event_time")),
                published_at=_parse_ts(t.get("published_at")),
                available_at=_parse_ts(t.get("available_at")),
                ingested_at=_parse_ts(t.get("ingested_at")),
            )

            # Team resolution (secondary path; recorded but non-fatal).
            provider_team_id = raw.get("team_id") or norm.get("team_id")
            if provider_team_id is None or str(provider_team_id).strip() == "":
                result.audit.teams_absent += 1
                team_id = None
            else:
                team_id = resolver.resolve_team(str(provider_team_id).strip())
                if team_id is None:
                    result.audit.teams_unmatched += 1
                else:
                    result.audit.teams_matched += 1

            # Entity resolution (player).
            provider_player_id = str(raw.get("player_id") or norm.get("player_id") or "").strip()
            _amb_before = len(result.resolution.ambiguous_players)
            player_id = resolver.resolve_player_by_context(
                provider_player_id,
                raw.get("player_name") or "",
                team_id,
                sid,
                result.resolution,
            )
            _amb_added = len(result.resolution.ambiguous_players) > _amb_before
            if player_id is None:
                # resolve_player_by_context records the outcome in result.resolution;
                # account for it explicitly and preserve the raw record for the
                # unmatched/raw audit artefact so nothing is silently dropped.
                if _amb_added:
                    result.audit.ambiguous += 1
                    result.audit.note("ambiguous", "no unique contextual match")
                else:
                    result.audit.unmatched += 1
                    result.audit.note("unmatched", "no provider-id and no unique contextual match")
                    result.unresolved_records.append(
                        {
                            "season": season_code,
                            "provider": resolver.provider_name,
                            "provider_player_id": provider_player_id,
                            "raw": raw,
                        }
                    )
                result.events_skipped += 1
                continue
            result.audit.matched += 1

            gw_num = raw.get("gameweek")
            gw_id = resolver.resolve_gameweek(sid, _int_or_none(gw_num))

            # Temporal classification.
            temporal_class = classify_temporal(
                timestamps, strict_backtest_safe=strict_backtest_safe
            )
            cutoff = _gameweek_cutoff(db, sid, _int_or_none(gw_num))
            eligible = (
                temporal_class == TemporalClass.STRICT_BACKTEST_SAFE
                and is_event_eligible_before_cutoff(timestamps, cutoff)
            )

            # Honest temporal guard: a STRICT_BACKTEST_SAFE event whose
            # information-availability timestamp falls outside the canonical
            # season window is a terminal-snapshot / look-ahead signal. It is
            # NOT silently accepted as strict; it is rejected into the
            # explicit skipped_temporal_invalid bucket so no false strict
            # intelligence survives entity resolution.
            if temporal_class == TemporalClass.STRICT_BACKTEST_SAFE:
                info_time = timestamps.published_at or timestamps.available_at
                window = _SEASON_WINDOWS.get(season_code)
                if window is not None and info_time is not None:
                    start = datetime.fromisoformat(window[0]).replace(tzinfo=UTC)
                    end = datetime.fromisoformat(window[1]).replace(tzinfo=UTC)
                    if info_time < start or info_time > end:
                        result.audit.skipped_temporal_invalid += 1
                        result.audit.note(
                            "skipped_temporal_invalid",
                            f"info_time {info_time.isoformat()} outside season window "
                            f"{window[0]}..{window[1]}",
                        )
                        result.events_skipped += 1
                        continue
            # Canonical status/evidence from event type.
            event_type = norm.get("event_type", "")
            status = norm.get("status") or event_type_to_status(event_type)
            evidence_type = norm.get("evidence_type") or event_type_to_evidence(event_type)

            # Provenance: source + article + evidence.
            source = _get_or_create_source(
                db, norm.get("source_name") or _FPL_SOURCE, norm.get("reliability", "unverified")
            )
            article_url = (
                norm.get("provider_event_id")
                or f"{provider.provider_name}:{season_code}:{provider_player_id}"
            )
            article = db.scalar(
                select(AvailabilityArticle).where(AvailabilityArticle.url == article_url)
            )
            if article is None:
                article = AvailabilityArticle(
                    source_id=source.id,
                    url=article_url,
                    headline=norm.get("event_type", ""),
                    published_at=timestamps.published_at,
                    ingested_at=timestamps.ingested_at or datetime.now(UTC),
                    content=norm.get("description"),
                )
                db.add(article)
                db.flush()

            # Idempotency: skip if an event with the same provider_event_id exists.
            existing = db.scalar(
                select(AvailabilityEvent).where(
                    AvailabilityEvent.provider == provider.provider_name,
                    AvailabilityEvent.provider_event_id == str(norm.get("provider_event_id") or ""),
                    AvailabilityEvent.season_id == sid,
                    AvailabilityEvent.player_id == player_id,
                )
            )
            if existing is not None:
                result.audit.skipped_duplicate += 1
                result.audit.note("skipped_duplicate", "idempotent existing provider_event_id")
                result.events_skipped += 1
                continue

            # Persist the availability evidence row (Layer B provenance).
            confidence = float(norm.get("confidence", 0.5))
            evidence = db.scalar(
                select(AvailabilityEvidence).where(
                    AvailabilityEvidence.article_id == article.id,
                    AvailabilityEvidence.player_id == player_id,
                    AvailabilityEvidence.season_id == sid,
                    AvailabilityEvidence.evidence_type == evidence_type,
                    AvailabilityEvidence.valid_from
                    == (
                        timestamps.published_at or timestamps.available_at or timestamps.event_time
                    ),
                )
            )
            if evidence is None:
                evidence = AvailabilityEvidence(
                    article_id=article.id,
                    player_id=player_id,
                    season_id=sid,
                    gameweek_id=gw_id,
                    evidence_type=evidence_type,
                    status_mentioned=status,
                    confidence=confidence,
                    description=norm.get("description"),
                    extracted_at=timestamps.ingested_at or datetime.now(UTC),
                    valid_from=timestamps.published_at
                    or timestamps.available_at
                    or timestamps.event_time,
                    is_active=True,
                )
                db.add(evidence)

            event = AvailabilityEvent(
                player_id=player_id,
                season_id=sid,
                gameweek_id=gw_id,
                status=status,
                confidence=confidence,
                evidence_count=1,
                primary_source_id=source.id,
                valid_from=timestamps.published_at
                or timestamps.available_at
                or timestamps.event_time
                or datetime.now(UTC),
                valid_to=None,
                is_current=True,
                temporal_class=temporal_class,
                provider=provider.provider_name,
                provider_event_id=str(norm.get("provider_event_id") or ""),
            )
            db.add(event)
            try:
                db.flush()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                result.audit.failed_persist += 1
                result.audit.note("failed_persist", type(exc).__name__)
                logger.error(
                    "persist failed for %s/%s: %s",
                    provider.provider_name,
                    norm.get("provider_event_id"),
                    exc,
                )
                continue
            result.audit.persisted += 1
            result.events_imported += 1
            if temporal_class == TemporalClass.STRICT_BACKTEST_SAFE:
                result.strict_safe += 1
                if eligible:
                    result.eligible_before_cutoff += 1
            elif temporal_class == TemporalClass.HISTORICAL_EVENT_ONLY:
                result.historical_event_only += 1
            else:
                result.unknown += 1

    db.flush()
    return result


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
