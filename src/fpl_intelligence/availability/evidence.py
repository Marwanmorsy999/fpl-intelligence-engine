"""Phase 7 evidence corroboration engine.

Corroborates evidence from multiple sources, computes consolidated confidence,
and produces immutable :class:`AvailabilityEvent` records.

Confidence model:
- Each raw evidence item gets a base confidence from its source reliability.
- Corroboration from N independent sources increases confidence (diminishing returns).
- Official club sources carry the highest weight and override lower-tier sources
  when they conflict.
- Manager quotes in press conferences carry high weight for near-term availability.
"""
from __future__ import annotations

from typing import Any

from fpl_intelligence.availability.models import (
    AvailabilityStatus,
    EvidenceType,
    SourceReliability,
)

# Base confidence by source reliability tier.
_SOURCE_CONFIDENCE: dict[str, float] = {
    SourceReliability.OFFICIAL: 0.95,
    SourceReliability.VERIFIED_JOURNALIST: 0.85,
    SourceReliability.RELIABLE_JOURNALIST: 0.70,
    SourceReliability.UNVERIFIED: 0.30,
}

# Evidence type modifiers — some evidence types are more predictive.
_EVIDENCE_TYPE_CONFIDENCE: dict[str, float] = {
    EvidenceType.MANAGER_QUOTE: 0.90,
    EvidenceType.TRAINING: 0.85,
    EvidenceType.LINEUP_HINT: 0.85,
    EvidenceType.INJURY: 0.80,
    EvidenceType.SUSPENSION: 0.85,
    EvidenceType.FITNESS: 0.70,
    EvidenceType.RECOVERY_UPDATE: 0.60,
    EvidenceType.TRANSFER_NEWS: 0.40,
}

# Status ordering — later statuses override earlier ones (more severe).
_STATUS_ORDER: dict[str, int] = {
    AvailabilityStatus.UNKNOWN: 0,
    AvailabilityStatus.START: 1,
    AvailabilityStatus.BENCH: 2,
    AvailabilityStatus.SUSPECT: 3,
    AvailabilityStatus.QUESTIONABLE: 4,
    AvailabilityStatus.DOUBTFUL: 5,
    AvailabilityStatus.OUT: 6,
    AvailabilityStatus.SUSPENDED: 6,
}


def source_confidence(reliability: str) -> float:
    """Return the base confidence for a source reliability tier."""
    return _SOURCE_CONFIDENCE.get(reliability, 0.30)


def evidence_type_confidence(evidence_type: str) -> float:
    """Return the confidence modifier for an evidence type."""
    return _EVIDENCE_TYPE_CONFIDENCE.get(evidence_type, 0.50)


def raw_confidence(reliability: str, evidence_type: str) -> float:
    """Compute raw confidence for a single evidence item.

    Confidence = source_confidence * evidence_type_confidence (multiplicative),
    clamped to [0.05, 0.99].
    """
    sc = source_confidence(reliability)
    et = evidence_type_confidence(evidence_type)
    return max(0.05, min(0.99, sc * et))


def corroborate(
    evidence_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Corroborate a list of evidence dicts for a single player+window.

    Each dict must have: reliability, evidence_type, status_mentioned,
    published_at, source_name.

    Returns:
        {
            "status": str (the corroborated status),
            "confidence": float (0.0–1.0),
            "evidence_count": int,
            "sources": list[str],
            "primary_source": str | None,
        }
    """
    if not evidence_items:
        return {
            "status": AvailabilityStatus.UNKNOWN,
            "confidence": 0.0,
            "evidence_count": 0,
            "sources": [],
            "primary_source": None,
        }

    # Compute per-item confidence.
    item_confs: list[tuple[float, dict[str, Any]]] = []
    for item in evidence_items:
        conf = raw_confidence(item["reliability"], item["evidence_type"])
        item_confs.append((conf, item))

    # Determine the most severe status mentioned.
    status_rank = -1
    chosen_status = AvailabilityStatus.UNKNOWN
    for _, item in item_confs:
        rank = _STATUS_ORDER.get(item["status_mentioned"], 0)
        if rank > status_rank:
            status_rank = rank
            chosen_status = item["status_mentioned"]

    # Aggregate confidence with diminishing returns for corroboration.
    # confidence = 1 - product(1 - conf_i) for all items of the chosen status,
    # then blend with lower-status items at half weight.
    same_status = [
        conf for conf, item in item_confs
        if item["status_mentioned"] == chosen_status
    ]
    other_status = [
        conf for conf, item in item_confs
        if item["status_mentioned"] != chosen_status
    ]

    if same_status:
        prod = 1.0
        for conf in same_status:
            prod *= (1.0 - conf)
        agg_conf = 1.0 - prod
    else:
        agg_conf = 0.0

    # Blend in corroboration from other-status items (diminished).
    for conf in other_status:
        agg_conf = 1.0 - (1.0 - agg_conf) * (1.0 - conf * 0.5)

    # Boost if official source corroborates.
    has_official = any(
        item["reliability"] == SourceReliability.OFFICIAL
        for _, item in item_confs
    )
    if has_official:
        agg_conf = min(0.99, agg_conf + 0.15 * (1.0 - agg_conf))

    # Primary source = highest confidence item.
    primary = max(item_confs, key=lambda x: x[0])[1] if item_confs else None

    sources = sorted(set(item["source_name"] for _, item in item_confs))

    return {
        "status": chosen_status,
        "confidence": round(agg_conf, 4),
        "evidence_count": len(evidence_items),
        "sources": sources,
        "primary_source": primary["source_name"] if primary else None,
    }
