"""Phase 9 temporal ledger — the single point where time enters the pipeline.

Phase 7's empirical blockage had one root cause that this module exists to make
structurally impossible: the availability source's timestamp
(``players_raw.csv``'s ``news_added``) was a **terminal season-end snapshot**,
i.e. a look-ahead signal masquerading as a publication time. Nothing in the
pipeline could tell the difference.

The ledger fixes that by:

1. **Refusing to fabricate.** ``published_at`` is nullable. If a source does not
   give us a real publication instant, we do not invent one — we record
   ``publication_established = False`` and fall back to the capture time.

2. **Deriving ``available_at`` conservatively.** Under the default policy,
   ``available_at = max(published_at, scraped_at)``. It is therefore never
   *earlier* than the moment we could actually have obtained the text. A
   generous ``PUBLICATION_TRUSTED`` policy exists but must be opted into per
   source, and it still can never precede ``published_at``.

3. **Validating the ordering invariants** before anything is written.

4. **Deciding deadline eligibility with the existing Phase 3 policy**
   (:class:`~fpl_intelligence.features.temporal.InformationAccessPolicy`)
   rather than a new, divergent rule.

Ordering invariants
-------------------

* every timestamp is timezone-aware
* ``published_at <= scraped_at``     (cannot capture before publication)
* ``scraped_at   <= ingested_at``    (cannot ledger before capture)
* ``published_at <= available_at``   (cannot be available before publication)
* ``available_at <= ingested_at``    (cannot claim access before we held it)
* no timestamp is in the future relative to the injected clock

``event_time`` is deliberately *not* ordered against the others: a press
conference published today can legitimately describe a future absence.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.features.temporal import InformationAccessPolicy
from fpl_intelligence.live_intelligence.models import (
    LedgerTemporalClass,
    LiveIntelligenceRawItem,
)

#: Injectable clock, so ingestion and tests are deterministic.
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Default clock: timezone-aware UTC now."""
    return datetime.now(UTC)


_WHITESPACE_RE = re.compile(r"\s+")


class TemporalIntegrityError(ValueError):
    """Raised when a temporal invariant would be violated.

    This is a hard failure by design. A ledger row that cannot prove its own
    temporal ordering is worse than no row at all, because it would silently
    contaminate every downstream backtest.
    """


class AvailabilityDerivationPolicy(StrEnum):
    """How ``available_at`` is derived from ``published_at`` / ``scraped_at``."""

    #: ``max(published_at, scraped_at)``. We only claim access from the moment
    #: we could genuinely have had the text in hand. This is the default and
    #: the only policy safe for an un-audited source.
    CONSERVATIVE = "conservative"
    #: ``published_at`` when present, else ``scraped_at``. Models an idealised
    #: system that polls the source continuously. Requires the source to be
    #: marked ``publication_timestamp_trusted``.
    PUBLICATION_TRUSTED = "publication_trusted"


# ---------------------------------------------------------------------------
# Timestamp value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerTimestamps:
    """The immutable temporal footprint of one ledger row."""

    scraped_at: datetime
    ingested_at: datetime
    available_at: datetime
    published_at: datetime | None = None
    event_time: datetime | None = None

    @property
    def publication_established(self) -> bool:
        """True only when a genuine publication instant was obtained."""
        return self.published_at is not None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "event_time": self.event_time.isoformat() if self.event_time else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "scraped_at": self.scraped_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Derivation and validation
# ---------------------------------------------------------------------------


def _require_aware(name: str, value: datetime | None) -> datetime | None:
    """Reject naive datetimes outright rather than guessing a timezone."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise TemporalIntegrityError(
            f"{name} must be timezone-aware; got naive datetime {value!r}. "
            "Naive timestamps are rejected because their true instant is unknowable."
        )
    return value


def derive_available_at(
    published_at: datetime | None,
    scraped_at: datetime,
    policy: AvailabilityDerivationPolicy = AvailabilityDerivationPolicy.CONSERVATIVE,
) -> datetime:
    """Derive the earliest instant we can legitimately claim access to the text.

    Never returns a value earlier than ``published_at``. Under the default
    conservative policy it never returns a value earlier than ``scraped_at``
    either, which is what makes back-dating impossible.
    """
    _require_aware("scraped_at", scraped_at)
    _require_aware("published_at", published_at)

    if published_at is None:
        return scraped_at
    if policy == AvailabilityDerivationPolicy.PUBLICATION_TRUSTED:
        return published_at
    return max(published_at, scraped_at)


def validate_timestamps(
    timestamps: LedgerTimestamps,
    *,
    now: datetime | None = None,
    allow_future_event_time: bool = True,
) -> LedgerTimestamps:
    """Validate the ledger ordering invariants, raising on any violation.

    Returns the same object on success so it can be used inline.
    """
    ts = timestamps
    _require_aware("scraped_at", ts.scraped_at)
    _require_aware("ingested_at", ts.ingested_at)
    _require_aware("available_at", ts.available_at)
    _require_aware("published_at", ts.published_at)
    _require_aware("event_time", ts.event_time)

    if ts.published_at is not None and ts.published_at > ts.scraped_at:
        raise TemporalIntegrityError(
            f"published_at ({ts.published_at.isoformat()}) is after scraped_at "
            f"({ts.scraped_at.isoformat()}): the text cannot have been captured "
            "before it was published."
        )
    if ts.scraped_at > ts.ingested_at:
        raise TemporalIntegrityError(
            f"scraped_at ({ts.scraped_at.isoformat()}) is after ingested_at "
            f"({ts.ingested_at.isoformat()}): the row cannot be ledgered before capture."
        )
    if ts.published_at is not None and ts.available_at < ts.published_at:
        raise TemporalIntegrityError(
            f"available_at ({ts.available_at.isoformat()}) precedes published_at "
            f"({ts.published_at.isoformat()}): information cannot be available "
            "before it exists."
        )
    if ts.available_at > ts.ingested_at:
        raise TemporalIntegrityError(
            f"available_at ({ts.available_at.isoformat()}) is after ingested_at "
            f"({ts.ingested_at.isoformat()}): we cannot ledger information we "
            "could not yet access."
        )

    if now is not None:
        _require_aware("now", now)
        for label, value in (
            ("published_at", ts.published_at),
            ("scraped_at", ts.scraped_at),
            ("available_at", ts.available_at),
            ("ingested_at", ts.ingested_at),
        ):
            if value is not None and value > now:
                raise TemporalIntegrityError(
                    f"{label} ({value.isoformat()}) is in the future relative to the "
                    f"pipeline clock ({now.isoformat()}). Back-dating and "
                    "forward-dating are both rejected."
                )
        if not allow_future_event_time and ts.event_time is not None and ts.event_time > now:
            raise TemporalIntegrityError(
                f"event_time ({ts.event_time.isoformat()}) is in the future."
            )
    return ts


def build_timestamps(
    *,
    scraped_at: datetime,
    ingested_at: datetime,
    published_at: datetime | None = None,
    event_time: datetime | None = None,
    availability_policy: AvailabilityDerivationPolicy = (AvailabilityDerivationPolicy.CONSERVATIVE),
    now: datetime | None = None,
) -> LedgerTimestamps:
    """Derive ``available_at`` and return a validated :class:`LedgerTimestamps`."""
    available_at = derive_available_at(published_at, scraped_at, availability_policy)
    return validate_timestamps(
        LedgerTimestamps(
            scraped_at=scraped_at,
            ingested_at=ingested_at,
            available_at=available_at,
            published_at=published_at,
            event_time=event_time,
        ),
        now=now,
    )


# ---------------------------------------------------------------------------
# Deadline eligibility
# ---------------------------------------------------------------------------


def is_usable_for_deadline(
    timestamps: LedgerTimestamps,
    deadline: datetime | None,
    policy: InformationAccessPolicy = InformationAccessPolicy.STRICT_REPRODUCIBILITY,
) -> bool:
    """Return True when the row satisfies ``policy`` against the FPL deadline.

    Mirrors :func:`fpl_intelligence.features.temporal.apply_policy` exactly, in
    Python rather than SQL, so the in-memory and database paths cannot diverge:

    * ``PUBLIC_AVAILABILITY``     -> ``available_at <= deadline``
    * ``SYSTEM_AVAILABILITY``     -> ``ingested_at <= deadline``
    * ``STRICT_REPRODUCIBILITY``  -> both (default)

    An absent deadline yields ``False``. Undecided is never treated as usable.
    """
    if deadline is None:
        return False
    _require_aware("deadline", deadline)

    if policy == InformationAccessPolicy.PUBLIC_AVAILABILITY:
        return timestamps.available_at <= deadline
    if policy == InformationAccessPolicy.SYSTEM_AVAILABILITY:
        return timestamps.ingested_at <= deadline
    if policy == InformationAccessPolicy.STRICT_REPRODUCIBILITY:
        return timestamps.available_at <= deadline and timestamps.ingested_at <= deadline
    raise ValueError(f"Unknown information-access policy: {policy}")


def classify_ledger_entry(
    timestamps: LedgerTimestamps,
    deadline: datetime | None,
    policy: InformationAccessPolicy = InformationAccessPolicy.STRICT_REPRODUCIBILITY,
) -> LedgerTemporalClass:
    """Classify a ledger row's deadline eligibility from its timestamps alone.

    This deliberately says nothing about whether the row is real or mock. That
    axis is carried by the source's ``environment`` marker and combined in
    :func:`is_validation_evidence`, so a well-timed mock row can never be
    promoted to evidence by accident.
    """
    if deadline is None:
        return LedgerTemporalClass.NO_DEADLINE_CONTEXT
    if is_usable_for_deadline(timestamps, deadline, policy):
        return LedgerTemporalClass.PRE_DEADLINE
    return LedgerTemporalClass.POST_DEADLINE


def is_validation_evidence(
    temporal_class: str,
    environment: str,
    *,
    is_mock_extraction: bool = False,
) -> bool:
    """Return True only for real, pre-deadline, non-mock-extracted material.

    The three axes must all pass. This is the single predicate that any future
    empirical report must use before counting a row as evidence.
    """
    return (
        temporal_class == LedgerTemporalClass.PRE_DEADLINE
        and environment == "real"
        and not is_mock_extraction
    )


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """Collapse whitespace and strip, for hashing and quote grounding."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def content_hash(text: str) -> str:
    """SHA-256 of the whitespace-normalised text.

    Used as the idempotency key so that re-capturing the same press conference
    appends nothing instead of duplicating the ledger.
    """
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Ledger service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerItemView:
    """Read-only projection of a ledger row handed to the LLM extractor.

    The extractor receives *only* this. It cannot see outcomes, later rows, or
    anything else in the database, which is what makes look-ahead leakage
    impossible at the reasoning layer rather than merely discouraged.
    """

    raw_item_id: int | None
    raw_text: str
    title: str | None
    source_name: str
    source_type: str
    source_reliability: str
    environment: str
    timestamps: LedgerTimestamps
    temporal_class: str
    season_id: int | None = None
    gameweek_id: int | None = None
    team_hint: str | None = None
    deadline_at: datetime | None = None


class TemporalLedger:
    """Append-only accessor over ``live_intelligence_raw_items``.

    The ledger never updates a row's temporal fields after insert. The only
    mutation it permits is attaching a previously-unknown gameweek deadline,
    which re-classifies ``NO_DEADLINE_CONTEXT`` rows — and even that can only
    move a row *out of* undecided, never from ``POST_DEADLINE`` to
    ``PRE_DEADLINE``.
    """

    def __init__(
        self,
        db: Session,
        *,
        clock: Clock = utc_now,
        policy: InformationAccessPolicy = InformationAccessPolicy.STRICT_REPRODUCIBILITY,
    ) -> None:
        self._db = db
        self._clock = clock
        self._policy = policy

    @property
    def policy(self) -> InformationAccessPolicy:
        return self._policy

    def now(self) -> datetime:
        return self._clock()

    # -- reads -------------------------------------------------------------

    def find_by_hash(self, source_id: int, hash_value: str) -> LiveIntelligenceRawItem | None:
        """Return the existing row for this source+content, if any."""
        return self._db.scalar(
            select(LiveIntelligenceRawItem).where(
                LiveIntelligenceRawItem.source_id == source_id,
                LiveIntelligenceRawItem.content_hash == hash_value,
            )
        )

    def items_available_before(
        self,
        cutoff: datetime,
        *,
        policy: InformationAccessPolicy | None = None,
        gameweek_id: int | None = None,
    ) -> list[LiveIntelligenceRawItem]:
        """Return ledger rows legitimately accessible at ``cutoff``.

        This is the only sanctioned way to read the ledger for decision-making.
        Reading the table directly bypasses the no-look-ahead filter.
        """
        _require_aware("cutoff", cutoff)
        effective = policy or self._policy
        stmt = select(LiveIntelligenceRawItem)

        if effective == InformationAccessPolicy.PUBLIC_AVAILABILITY:
            stmt = stmt.where(LiveIntelligenceRawItem.available_at <= cutoff)
        elif effective == InformationAccessPolicy.SYSTEM_AVAILABILITY:
            stmt = stmt.where(LiveIntelligenceRawItem.ingested_at <= cutoff)
        else:
            stmt = stmt.where(
                LiveIntelligenceRawItem.available_at <= cutoff,
                LiveIntelligenceRawItem.ingested_at <= cutoff,
            )
        if gameweek_id is not None:
            stmt = stmt.where(LiveIntelligenceRawItem.gameweek_id == gameweek_id)

        return list(self._db.execute(stmt.order_by(LiveIntelligenceRawItem.available_at)).scalars())

    def to_view(self, item: LiveIntelligenceRawItem) -> LedgerItemView:
        """Project a persisted row into the read-only extractor view."""
        return LedgerItemView(
            raw_item_id=item.id,
            raw_text=item.raw_text,
            title=item.title,
            source_name=item.source.name,
            source_type=item.source.source_type,
            source_reliability=item.source.reliability,
            environment=item.source.environment,
            timestamps=LedgerTimestamps(
                scraped_at=_as_utc(item.scraped_at),
                ingested_at=_as_utc(item.ingested_at),
                available_at=_as_utc(item.available_at),
                published_at=_as_utc_opt(item.published_at),
                event_time=_as_utc_opt(item.event_time),
            ),
            temporal_class=item.temporal_class,
            season_id=item.season_id,
            gameweek_id=item.gameweek_id,
            team_hint=item.team_hint,
            deadline_at=_as_utc_opt(item.deadline_at),
        )

    # -- deadline attachment ----------------------------------------------

    def attach_deadline(
        self,
        item: LiveIntelligenceRawItem,
        deadline: datetime,
        *,
        policy: InformationAccessPolicy | None = None,
    ) -> LedgerTemporalClass:
        """Attach a resolved gameweek deadline and (re)classify the row.

        Only rows currently in ``NO_DEADLINE_CONTEXT`` may be classified. A row
        that has already been judged ``POST_DEADLINE`` is never re-opened,
        which removes the obvious back-door into look-ahead.
        """
        _require_aware("deadline", deadline)
        if item.temporal_class != LedgerTemporalClass.NO_DEADLINE_CONTEXT:
            raise TemporalIntegrityError(
                f"Ledger row {item.id} is already classified as "
                f"'{item.temporal_class}'. Re-classifying a decided row is "
                "forbidden: it is the classic route to look-ahead leakage."
            )
        effective = policy or self._policy
        timestamps = LedgerTimestamps(
            scraped_at=_as_utc(item.scraped_at),
            ingested_at=_as_utc(item.ingested_at),
            available_at=_as_utc(item.available_at),
            published_at=_as_utc_opt(item.published_at),
            event_time=_as_utc_opt(item.event_time),
        )
        temporal_class = classify_ledger_entry(timestamps, deadline, effective)
        item.deadline_at = deadline
        item.temporal_class = temporal_class
        item.access_policy = str(effective)
        return temporal_class


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime read back from SQLite."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_utc_opt(value: datetime | None) -> datetime | None:
    return None if value is None else _as_utc(value)
