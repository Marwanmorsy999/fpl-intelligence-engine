"""Phase 9 — Live Intelligence Accumulator & LLM Analyst Layer.

Phase 9 exists because Phase 7 (Availability) and Phase 8 (Tactics) are
engineering-complete but **empirically blocked**: no historical archive of
pre-deadline, unstructured football intelligence exists that can pass
``InformationAccessPolicy.STRICT_REPRODUCIBILITY``. The only remedy is to start
accumulating that data **forward in time**, from now, with honest timestamps.

Flow::

    Raw Text
        -> Temporal Ledger        (live_intelligence_raw_items)
        -> LLM Extractor          (strictly typed JSON, grounded in the text)
        -> Structured Evidence    (availability_evidence + tactical_evidence)
        -> AI Analyst             (narrative synthesis, cites quant vs qual)

Architectural non-negotiables enforced by this package
------------------------------------------------------

1. **Strict separation.** The quantitative engine (Phases 1-6) is never
   modified, never re-fitted, and never overwritten by this layer. The AI
   Analyst consumes :class:`~fpl_intelligence.optimization.provider.PlayerPrediction`
   read-only and is structurally incapable of emitting a revised point
   projection (see :mod:`fpl_intelligence.live_intelligence.analyst`).

2. **LLM is the reasoning layer, not the prediction engine.** The LLM converts
   unstructured text into typed evidence and writes prose. It never produces a
   number that feeds the optimizer.

3. **No look-ahead is structurally impossible.** The LLM never supplies a
   timestamp. Every temporal field on extracted evidence is *inherited* from
   the immutable ledger row that the text came from. Deadline eligibility is
   decided by :mod:`fpl_intelligence.live_intelligence.temporal_ledger` using
   the existing Phase 3 :class:`InformationAccessPolicy`.

4. **Mock is never evidence.** Sources carry a ``DataEnvironment`` marker.
   Mock-environment ledger rows can never be reported as real validation
   evidence, regardless of their temporal class.

5. **Nothing is silently dropped.** Unresolved players, ungrounded quotes and
   schema-rejected payloads are recorded with a reason, not discarded.
"""

from fpl_intelligence.live_intelligence.models import (
    CaptureMethod,
    ExtractionStatus,
    LedgerTemporalClass,
    LiveAvailabilityEvidenceLink,
    LiveIntelligenceRawItem,
    LiveIntelligenceSource,
    LiveSourceType,
    LLMExtractionRun,
    TacticalDirection,
    TacticalEvidence,
    TacticalEvidenceType,
)
from fpl_intelligence.live_intelligence.temporal_ledger import (
    AvailabilityDerivationPolicy,
    LedgerTimestamps,
    TemporalIntegrityError,
    TemporalLedger,
    classify_ledger_entry,
    derive_available_at,
    is_usable_for_deadline,
)

__all__ = [
    "AvailabilityDerivationPolicy",
    "CaptureMethod",
    "ExtractionStatus",
    "LLMExtractionRun",
    "LedgerTemporalClass",
    "LedgerTimestamps",
    "LiveAvailabilityEvidenceLink",
    "LiveIntelligenceRawItem",
    "LiveIntelligenceSource",
    "LiveSourceType",
    "TacticalDirection",
    "TacticalEvidence",
    "TacticalEvidenceType",
    "TemporalIntegrityError",
    "TemporalLedger",
    "classify_ledger_entry",
    "derive_available_at",
    "is_usable_for_deadline",
]
