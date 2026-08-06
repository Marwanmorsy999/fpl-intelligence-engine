"""Environment / dataset provenance marker.

Distinguishes synthetic (mock) data from real historical data so that:

* mock results can never be labelled as real predictive validation;
* the Phase 4.5 gate refuses to treat synthetic results as real evidence;
* real and synthetic records are never silently mixed (except in explicit
  integration tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class DataEnvironment(str, Enum):
    """Marker for the origin of a dataset."""

    MOCK = "mock"
    REAL = "real"


class DatasetClass(str, Enum):
    """Temporal integrity classification (Section 9/10 of Phase 4.75)."""

    #: Availability of every record can be reasonably established and it may be
    #: used as a leakage-free feature in strict backtests.
    STRICT_BACKTEST_SAFE = "STRICT_BACKTEST_SAFE"
    #: Historically useful but exact availability cannot be reconstructed.
    #: Usable as an outcome or for exploratory analysis only -- never as a
    #: strict pre-deadline feature.
    HISTORICAL_OUTCOME_ONLY = "HISTORICAL_OUTCOME_ONLY"


@dataclass
class DatasetMarker:
    """Declarative, machine-readable marker for a dataset.

    ``snapshot_timing`` documents precisely what a temporal snapshot represents
    (e.g. ``"gameweek_end"`` vs ``"pre_deadline"``) and must never be mislabeled.
    """

    provider: str
    environment: DataEnvironment
    dataset: str
    temporal_class: DatasetClass
    snapshot_timing: str | None = None
    source_url: str | None = None
    retrieval_timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "environment": self.environment.value,
            "dataset": self.dataset,
            "temporal_class": self.temporal_class.value,
            "snapshot_timing": self.snapshot_timing,
            "source_url": self.source_url,
            "retrieval_timestamp": self.retrieval_timestamp.isoformat(),
            "notes": self.notes,
        }


# Copyright / licensing / provenance book-keeping helper.
@dataclass
class SourceProvenance:
    """Attach provenance metadata to a dataset source."""

    provider: str
    environment: DataEnvironment
    source_name: str
    url: str | None
    access_method: str
    retrieval_date: str
    license_notes: str
    fields_provided: list[str] = field(default_factory=list)
    seasons_covered: list[str] = field(default_factory=list)
    known_limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "environment": self.environment.value,
            "source_name": self.source_name,
            "url": self.url,
            "access_method": self.access_method,
            "retrieval_date": self.retrieval_date,
            "license_notes": self.license_notes,
            "fields_provided": self.fields_provided,
            "seasons_covered": self.seasons_covered,
            "known_limitations": self.known_limitations,
        }


def merge_environments(envs: list[DataEnvironment]) -> DataEnvironment:
    """Return a single environment marker for a set of records.

    Mixing real and mock data outside an explicit integration test is the
    precise failure this function exists to prevent.
    """
    as_set = set(envs)
    if len(as_set) > 1:
        raise ValueError(
            "Cannot merge mixed environments (mock + real) without an explicit "
            "integration-test context."
        )
    return as_set.pop() if as_set else DataEnvironment.MOCK
