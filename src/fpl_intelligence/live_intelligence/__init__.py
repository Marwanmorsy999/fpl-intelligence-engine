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

6. **Real providers are guarded, not trusted.** Phase 9.1 adds live Gemini /
   Groq / OpenRouter access behind a response cache, a hard ``max_tokens``
   cap, a rate limiter and a per-process call budget. Credentials come from a
   git-ignored ``.env`` only, and every extraction carries the SHA-256 of the
   prompt template that produced it (see
   :mod:`fpl_intelligence.live_intelligence.prompt_registry`).
"""

from fpl_intelligence.live_intelligence.bridge import (
    AnalystReportGenerator,
    EvidenceQueryResult,
    EvidenceQueryService,
    PredictionContextBuilder,
    StaticPredictionProvider,
)
from fpl_intelligence.live_intelligence.llm_settings import (
    API_KEY_ENV_VAR,
    DEFAULT_MODELS,
    LLMProviderName,
    LLMSettings,
    MissingAPIKeyError,
    MissingEnvFileError,
    get_llm_settings,
    load_llm_settings,
)
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
from fpl_intelligence.live_intelligence.prompt_registry import (
    PROMPT_HASH_LOCK,
    PromptFingerprint,
    fingerprint_prompt,
    fingerprint_template,
    hash_prompt_template,
    verify_prompt_registry,
)
from fpl_intelligence.live_intelligence.provider_router import (
    DEFAULT_TASK_ROUTES,
    ProviderRouter,
    ProviderRoutingError,
    RouteDecision,
    RouteFailure,
    RoutingStrategy,
)
from fpl_intelligence.live_intelligence.rate_limit import (
    CallBudget,
    CallBudgetExceededError,
    RateLimiter,
)
from fpl_intelligence.live_intelligence.report import (
    IntelligenceReport,
    PredictionContext,
    ReportConfidence,
    ReportEvidenceCitation,
    ReportQualitativeAdjustment,
    ReportQuantitativeBaseline,
    UnresolvedWarning,
)
from fpl_intelligence.live_intelligence.response_cache import (
    CacheEntry,
    InMemoryResponseCache,
    NullResponseCache,
    ResponseCache,
    SqliteResponseCache,
    build_cache,
    make_cache_key,
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
    "API_KEY_ENV_VAR",
    "AnalystReportGenerator",
    "AvailabilityDerivationPolicy",
    "CacheEntry",
    "CallBudget",
    "CallBudgetExceededError",
    "CaptureMethod",
    "DEFAULT_MODELS",
    "DEFAULT_TASK_ROUTES",
    "EvidenceQueryResult",
    "EvidenceQueryService",
    "ExtractionStatus",
    "InMemoryResponseCache",
    "IntelligenceReport",
    "LLMExtractionRun",
    "LLMProviderName",
    "LLMSettings",
    "LedgerTemporalClass",
    "LedgerTimestamps",
    "LiveAvailabilityEvidenceLink",
    "LiveIntelligenceRawItem",
    "LiveIntelligenceSource",
    "LiveSourceType",
    "MissingAPIKeyError",
    "MissingEnvFileError",
    "NullResponseCache",
    "PROMPT_HASH_LOCK",
    "PredictionContext",
    "PredictionContextBuilder",
    "PromptFingerprint",
    "ProviderRouter",
    "ProviderRoutingError",
    "RateLimiter",
    "ReportConfidence",
    "ReportEvidenceCitation",
    "ReportQualitativeAdjustment",
    "ReportQuantitativeBaseline",
    "ResponseCache",
    "RouteDecision",
    "RouteFailure",
    "RoutingStrategy",
    "SqliteResponseCache",
    "StaticPredictionProvider",
    "TacticalDirection",
    "TacticalEvidence",
    "TacticalEvidenceType",
    "TemporalIntegrityError",
    "TemporalLedger",
    "UnresolvedWarning",
    "build_cache",
    "classify_ledger_entry",
    "derive_available_at",
    "fingerprint_prompt",
    "fingerprint_template",
    "get_llm_settings",
    "hash_prompt_template",
    "is_usable_for_deadline",
    "load_llm_settings",
    "make_cache_key",
    "verify_prompt_registry",
]
