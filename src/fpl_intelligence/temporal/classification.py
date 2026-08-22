"""Temporal integrity classification for real datasets (Phase 4.75 Section 9).

For every real dataset we determine whether we can establish:

* ``event_time`` -- when the event happened
* ``published_at`` -- when the source published it
* ``available_at`` -- earliest time we can legitimately claim access
* ``ingested_at`` -- when our pipeline actually collected it

We never fabricate missing timestamps. Datasets are classified as either:

* ``STRICT_BACKTEST_SAFE`` -- availability can be reasonably established, may
  be used as a leakage-free feature in strict backtests; OR
* ``HISTORICAL_OUTCOME_ONLY`` -- historically useful but exact availability
  cannot be reconstructed; usable as an outcome or for exploratory analysis
  only, never as a strict pre-deadline feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fpl_intelligence.domain.environment import DatasetClass


@dataclass
class DatasetTemporalProfile:
    """Temporal classification for a single real dataset."""

    provider: str
    dataset: str
    temporal_class: DatasetClass
    event_time_established: bool
    published_at_established: bool
    available_at_established: bool
    ingested_at_established: bool
    rationale: str
    snapshot_timing: str | None = None


@dataclass
class TemporalProfile:
    """Aggregate temporal profile across all imported datasets."""

    profiles: list[DatasetTemporalProfile] = field(default_factory=list)

    def strict_safe(self) -> list[DatasetTemporalProfile]:
        return [p for p in self.profiles if p.temporal_class == DatasetClass.STRICT_BACKTEST_SAFE]

    def outcome_only(self) -> list[DatasetTemporalProfile]:
        return [
            p for p in self.profiles if p.temporal_class == DatasetClass.HISTORICAL_OUTCOME_ONLY
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "strict_backtest_safe": [p.__dict__ for p in self.strict_safe()],
            "historical_outcome_only": [p.__dict__ for p in self.outcome_only()],
        }


def classify_provider(provider_name: str, dataset: str) -> DatasetTemporalProfile:
    """Classify the temporal integrity of a real dataset.

    The vaastav FPL mirror's gameweek performance is the *finalized outcome*
    of a past Gameweek: it was genuinely published right after that Gameweek,
    so it is legitimately available as a feature for later Gameweeks
    (strict-safe via Gameweek ordering). The mirror's price/ownership snapshots
    are Gameweek-END values with no recorded pre-deadline availability, so they
    are HISTORICAL_OUTCOME_ONLY for snapshot-timing purposes.
    """
    if dataset == "fpl_snapshots":
        return DatasetTemporalProfile(
            provider=provider_name,
            dataset=dataset,
            temporal_class=DatasetClass.HISTORICAL_OUTCOME_ONLY,
            event_time_established=True,
            published_at_established=False,
            available_at_established=False,
            ingested_at_established=True,
            rationale=(
                "Mirror snapshots are gameweek-end values; no recorded pre-deadline "
                "availability. usable as outcome/exploratory only, NOT as a strict "
                "pre-deadline feature."
            ),
            snapshot_timing="gameweek_end (post-deadline); NOT pre-deadline",
        )
    if dataset == "fpl_history":
        return DatasetTemporalProfile(
            provider=provider_name,
            dataset=dataset,
            temporal_class=DatasetClass.STRICT_BACKTEST_SAFE,
            event_time_established=True,
            published_at_established=True,
            available_at_established=True,
            ingested_at_established=True,
            rationale=(
                "Finalized gameweek outcomes are published right after each gameweek; "
                "legitimately available as features for later gameweeks (strict via "
                "gameweek ordering)."
            ),
            snapshot_timing="gameweek outcome; available after each gameweek",
        )
    return DatasetTemporalProfile(
        provider=provider_name,
        dataset=dataset,
        temporal_class=DatasetClass.STRICT_BACKTEST_SAFE,
        event_time_established=True,
        published_at_established=True,
        available_at_established=True,
        ingested_at_established=True,
        rationale="Reference data (teams/fixtures) with stable identifiers.",
    )
