"""Historical availability provider adapters (Phase 7.2).

Provider adapters normalise external availability/injury sources into a common
raw-event dict shape consumed by the importer. The abstraction isolates the
source-specific handling so no single provider is hard-coded into Phase 7 models.

Providers:
- :class:`HistoricalAvailabilityProvider` — ABC defining the adapter contract.
- :class:`RealFPLAvailabilityProvider` — REAL source: derives availability
  events from the official FPL bootstrap ``news`` / ``news_added`` /
  ``chance_of_playing_*`` / ``status`` fields that are already cached in the
  vaastav mirror's ``players_raw.csv``. This is genuine availability
  intelligence with publication timestamps (``news_added``), downloadable and
  reproducible from the public mirror. It is NOT the mock provider.
- :class:`TransfermarktAvailabilityProvider` — adapter for Transfermarkt-derived
  injury/absence data (usage-permitted investigations only; not wired by default).
- :class:`PublicInjuryDatasetProvider` — adapter for public GitHub injury
  datasets (documented candidates only; not wired by default).
- :class:`ClubArchiveAvailabilityProvider` — adapter for club availability archives.
- :class:`PressConferenceArchiveProvider` — adapter for press-conference archives.
- :class:`SampleHistoricalAvailabilityProvider` — deterministic sample provider
  labelled ``MOCK / ENGINEERING VERIFICATION ONLY``. It is NEVER counted as real
  historical availability data.
"""
from __future__ import annotations

import abc
import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fpl_intelligence.availability.historical.event_types import parse_event_type
from fpl_intelligence.availability.historical.temporal import AvailabilityTimestamps

MANIFEST_LICENSE_NOTES = (
    "Public dataset mirrors for research use. Verify upstream licensing for "
    "any commercial use."
)


class HistoricalAvailabilityProvider(abc.ABC):
    """Abstract base for a historical availability source adapter."""

    environment: str = "real"

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Unique name for this provider."""

    @property
    @abc.abstractmethod
    def source_name(self) -> str:
        """Human-readable source name."""

    @property
    @abc.abstractmethod
    def seasons_covered(self) -> list[str]:
        """Seasons this provider can supply."""

    @abc.abstractmethod
    def fetch_events(self, season: str) -> list[dict[str, Any]]:
        """Return raw availability events for a season.

        Each raw event dict should contain at least:
            provider_event_id, season_code, player_id, team_id, event_type,
            status, description, timestamps (event_time/published_at/available_at),
            source_name, reliability.
        """


class RealFPLAvailabilityProvider(HistoricalAvailabilityProvider):
    """REAL historical availability provider derived from the FPL bootstrap.

    The official FPL bootstrap (mirrored per season as ``players_raw.csv`` in
    the public vaastav mirror) exposes per-player availability fields:
    - ``news``: free-text availability news (injury / suspension / loan / etc.)
    - ``news_added``: publication timestamp of the news (ISO 8601)
    - ``status``: 'a' (available) / 'd' (doubtful) / 'i' (injured) / 'u' (unavailable)
    - ``chance_of_playing_this_round`` / ``chance_of_playing_next_round``

    This is genuine availability intelligence with publication timestamps. The
    ``news_added`` timestamp establishes information availability time, so
    events carrying it can be STRICT_BACKTEST_SAFE when compared against the
    gameweek deadline. Entries without a usable timestamp are classified
    HISTORICAL_EVENT_ONLY (never silently strict).

    The bootstrap reflects the state *at the time the mirror's raw file was
    captured* (season-start for players_raw.csv). This is documented in the
    source audit: it is a point-in-time snapshot, not a full per-gameweek news
    history.
    """

    environment = "real"

    def __init__(self, raw_root: Path) -> None:
        self.raw_root = raw_root
        self._cache: dict[str, list[dict[str, str]]] = {}

    @property
    def provider_name(self) -> str:
        return "real_fpl_bootstrap"

    @property
    def source_name(self) -> str:
        return "FPL bootstrap (players_raw.csv) availability news"

    @property
    def seasons_covered(self) -> list[str]:
        return ["2022-23", "2023-24", "2024-25", "2025-26"]

    # -- loading ----------------------------------------------------------
    def _load_players_raw(self, season: str) -> list[dict[str, str]]:
        if season in self._cache:
            return self._cache[season]
        path = self.raw_root / "real_fpl" / season / "players" / "players_raw.csv"
        if not path.exists():
            self._cache[season] = []
            return []
        with path.open(encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(io.StringIO(fh.read()))]
        self._cache[season] = rows
        return rows

    # -- protocol ---------------------------------------------------------
    def fetch_events(self, season: str) -> list[dict[str, Any]]:
        rows = self._load_players_raw(season)
        events: list[dict[str, Any]] = []
        for row in rows:
            news_raw = (row.get("news") or "").strip()
            player_id = str(row.get("id", "") or "").strip()
            if not player_id:
                continue
            status_code = (row.get("status") or "a").strip()
            news_added = _parse_dt(row.get("news_added"))
            chance_this = _int_or_none(row.get("chance_of_playing_this_round"))

            # Default availability status from the official FPL 'status' code.
            status = _map_fpl_status(status_code)

            description = news_raw or f"chance_of_playing_this_round={chance_this}" if (
                chance_this not in (None, 100)
            ) else (news_raw or None)

            # Event type derived conservatively from the free-text news + status.
            labels = [news_raw, status_code]
            event_type = parse_event_type(labels) if news_raw else (
                _event_type_from_status(status_code)
            )

            timestamps = AvailabilityTimestamps(
                event_time=None,
                published_at=news_added,
                available_at=news_added,
                ingested_at=news_added,
            )

            events.append(
                {
                    "provider_event_id": f"{season}:{player_id}",
                    "provider": self.provider_name,
                    "season_code": season,
                    "player_id": player_id,
                    "team_id": str(row.get("team", "") or "").strip() or None,
                    "event_type": event_type,
                    "status": status,
                    "description": description,
                    "chance_of_playing_this_round": chance_this,
                    "chance_of_playing_next_round": _int_or_none(
                        row.get("chance_of_playing_next_round")
                    ),
                    "news_added": news_added,
                    "timestamps": timestamps,
                    "source_name": self.source_name,
                    "reliability": "official",
                }
            )
        return events


class _NotWiredProvider(HistoricalAvailabilityProvider):
    """Stub for candidate providers that are audited but NOT wired.

    These adapters document the intended shape and the access method; they raise
    a clear NotWiredError so downstream code can never accidentally treat them as
    production dependencies before a source passes the feasibility audit.
    """

    environment = "real"

    def __init__(self, seasons: list[str] | None = None):
        self._seasons = seasons or ["2022-23", "2023-24", "2024-25", "2025-26"]

    @property
    def seasons_covered(self) -> list[str]:
        return self._seasons

    def fetch_events(self, season: str) -> list[dict[str, Any]]:
        raise NotWiredError(
            f"{self.provider_name} is audited but NOT wired as a production "
            "dependency. It cannot be used until the feasibility audit and "
            "the source usage terms are resolved."
        )


class NotWiredError(RuntimeError):
    """Raised by candidate adapters that have not passed the feasibility audit."""


class TransfermarktAvailabilityProvider(_NotWiredProvider):
    """Adapter for Transfermarkt-derived injury/absence data (AUDIT ONLY)."""

    @property
    def provider_name(self) -> str:
        return "transfermarkt_availability"

    @property
    def source_name(self) -> str:
        return "Transfermarkt injury/absence data (audit-only)"


class PublicInjuryDatasetProvider(_NotWiredProvider):
    """Adapter for public GitHub injury datasets (AUDIT ONLY)."""

    @property
    def provider_name(self) -> str:
        return "public_injury_dataset"

    @property
    def source_name(self) -> str:
        return "Public GitHub injury dataset (audit-only)"


class ClubArchiveAvailabilityProvider(_NotWiredProvider):
    """Adapter for club availability archives (AUDIT ONLY)."""

    @property
    def provider_name(self) -> str:
        return "club_archive_availability"

    @property
    def source_name(self) -> str:
        return "Club availability archive (audit-only)"


class PressConferenceArchiveProvider(_NotWiredProvider):
    """Adapter for press-conference archives (AUDIT ONLY)."""

    @property
    def provider_name(self) -> str:
        return "press_conference_archive"

    @property
    def source_name(self) -> str:
        return "Press-conference archive (audit-only)"


class SampleHistoricalAvailabilityProvider(HistoricalAvailabilityProvider):
    """Deterministic sample provider for engineering verification.

    LABEL: MOCK / ENGINEERING VERIFICATION ONLY.

    This provider is used to verify the pipeline (normalization, temporal
    classification, entity resolution, persistence, idempotency, quality
    validation, migration 0007, coverage audit). It is NEVER counted as real
    historical availability data and is excluded from real coverage metrics and
    empirical Phase 7 classification.

    The sample is deterministic: the same season yields the same events across
    runs. ``environment`` is 'mock' so downstream metrics can exclude it.
    """

    environment = "mock"

    # Player primary IDs sampled from the real FPL mirror (deterministic).
    _PLAYER_IDS = [
        "44", "234", "233", "9", "13", "20", "3", "7", "192", "90",
    ]

    def __init__(self, seasons: list[str] | None = None) -> None:
        self._seasons = seasons or ["2022-23", "2023-24", "2024-25", "2025-26"]
        self._cache: dict[str, list[dict[str, Any]]] = {}

    @property
    def provider_name(self) -> str:
        return "sample_historical_availability"

    @property
    def source_name(self) -> str:
        return "sample_historical_availability (MOCK / ENGINEERING VERIFICATION ONLY)"

    @property
    def seasons_covered(self) -> list[str]:
        return self._seasons

    def fetch_events(self, season: str) -> list[dict[str, Any]]:
        if season not in self._cache:
            self._cache[season] = self._generate(season)
        return self._cache[season]

    def _generate(self, season: str) -> list[dict[str, Any]]:
        """Deterministically generate availability events for a season.

        Each player index maps to a deterministic verdict:
        0: injury with published_at (strict-safe)
        1: injury without publication timestamp (event-only)
        2: suspension (strict-safe)
        3: fully available (no event)
        4: doubtful (strict-safe)
        5: training-limited (strict-safe)
        6: no timestamp at all (UNKNOWN)
        7: injury with published_at after a mid-season cutoff (NOT eligible before cutoff,
           still strict-classified but excluded by the deadlineless eligibility check)
        8: available with publication timestamp
        9: illness without timestamp (event-only)
        """
        base_day = {
            "2022-23": datetime(2022, 8, 1, 10, 0, tzinfo=UTC),
            "2023-24": datetime(2023, 8, 1, 10, 0, tzinfo=UTC),
            "2024-25": datetime(2024, 8, 1, 10, 0, tzinfo=UTC),
            "2025-26": datetime(2025, 8, 1, 10, 0, tzinfo=UTC),
        }[season]

        plans = {
            0: ("injury", "Hamstring injury", "injury", True),
            1: ("injury", "Knee injury", "injury", False),
            2: ("suspension", "Red card suspension", "suspension", True),
            3: (None, None, None, None),
            4: ("doubtful", "Knock, doubtful", "doubtful", True),
            5: ("training_limited", "Limited in training", "training_limited", True),
            6: (None, None, None, None),
            7: ("injury", "Late injury news", "injury", True),
            8: ("available", "Back in full training", "available", True),
            9: ("illness", "Illness", "illness", False),
        }

        events: list[dict[str, Any]] = []
        for i, player_id in enumerate(self._PLAYER_IDS):
            etype, desc, status, has_pub = plans[i]
            if etype is None:
                continue
            if i == 6:
                # Unknown temporal — no timestamps at all.
                timestamps = AvailabilityTimestamps(
                    event_time=None, published_at=None,
                    available_at=None, ingested_at=None,
                )
            elif has_pub:
                published = base_day + timedelta(days=0 if i != 7 else 45)
                timestamps = AvailabilityTimestamps(
                    event_time=published,
                    published_at=published,
                    available_at=published,
                    ingested_at=published,
                )
            else:
                timestamps = AvailabilityTimestamps(
                    event_time=base_day,
                    published_at=None,
                    available_at=None,
                    ingested_at=None,
                )
            events.append(
                {
                    "provider_event_id": f"{season}:{player_id}",
                    "provider": self.provider_name,
                    "season_code": season,
                    "player_id": player_id,
                    "team_id": None,
                    "event_type": etype,
                    "status": status,
                    "description": desc,
                    "timestamps": timestamps,
                    "source_name": self.source_name,
                    "reliability": "unverified",
                    "environment": "mock",
                }
            )
        return events


def _map_fpl_status(status_code: str) -> str:
    """Map official FPL status code to a canonical AvailabilityStatus.

    The ``"a"`` code is "available" — fit and in contention, not a confirmed
    start — so it maps to :attr:`AvailabilityStatus.AVAILABLE`. This must agree
    with :func:`_event_type_from_status`, which maps ``"a"`` to
    ``HistoricalEventType.AVAILABLE`` (and therefore, via
    ``event_types.AVAILABLE -> AVAILABLE``, to the same canonical status). The
    previous ``"a" -> START`` was a split-brain: the same source code produced
    two different canonical statuses depending on which mapper read it.
    """
    from fpl_intelligence.availability.models import AvailabilityStatus

    return {
        "a": AvailabilityStatus.AVAILABLE,
        "d": AvailabilityStatus.DOUBTFUL,
        "i": AvailabilityStatus.OUT,
        "u": AvailabilityStatus.OUT,
    }.get(status_code, AvailabilityStatus.UNKNOWN)


def _event_type_from_status(status_code: str) -> str:
    from fpl_intelligence.availability.historical.event_types import HistoricalEventType

    return {
        "a": HistoricalEventType.AVAILABLE,
        "d": HistoricalEventType.DOUBTFUL,
        "i": HistoricalEventType.INJURY,
        "u": HistoricalEventType.INJURY,
    }.get(status_code, HistoricalEventType.UNKNOWN)


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
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
