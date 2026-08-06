"""Source reliability metadata for historical availability providers (Phase 7.2).

We NEVER invent historical accuracy scores without evidence. Initially store a
neutral prior (0.5) where historical validation is unavailable. Reliability
should later be learned from Phase 7 evaluation.

The metadata is persisted to the ``source_reliability_metadata`` table so the
import path can attach a source to every event and so the coverage audit can
report per-source reliability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NEUTRAL_PRIOR = 0.5


@dataclass
class SourceReliabilityMetadata:
    """Reliability metadata for a single historical availability source."""

    source_type: str
    source_name: str
    reliability_level: str
    event_types_supported: list[str] = field(default_factory=list)
    sample_size: int | None = None
    timestamp_reliability: str = "unknown"
    verified_accuracy: float | None = NEUTRAL_PRIOR
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_name": self.source_name,
            "reliability_level": self.reliability_level,
            "event_types_supported": list(self.event_types_supported),
            "sample_size": self.sample_size,
            "timestamp_reliability": self.timestamp_reliability,
            "verified_accuracy": self.verified_accuracy,
            "notes": self.notes,
        }


def neutral_reliability(source_type: str, source_name: str, **kwargs: Any) -> SourceReliabilityMetadata:
    """Build a SourceReliabilityMetadata with a neutral accuracy prior.

    ``verified_accuracy`` defaults to NEUTRAL_PRIOR (0.5) and is only ever set to
    a non-neutral value when real historical validation evidence exists. This
    function never fabricates historical accuracy.
    """
    return SourceReliabilityMetadata(
        source_type=source_type,
        source_name=source_name,
        reliability_level=kwargs.pop("reliability_level", "unverified"),
        event_types_supported=list(kwargs.pop("event_types_supported", [])),
        sample_size=kwargs.pop("sample_size", None),
        timestamp_reliability=kwargs.pop("timestamp_reliability", "unknown"),
        verified_accuracy=kwargs.pop("verified_accuracy", NEUTRAL_PRIOR),
        notes=kwargs.pop("notes", ""),
    )
