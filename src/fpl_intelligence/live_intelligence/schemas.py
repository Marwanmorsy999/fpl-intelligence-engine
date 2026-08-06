"""Phase 9 strictly-typed LLM I/O contracts.

Every byte that crosses the boundary between the LLM and the engine passes
through these Pydantic models. They are the enforcement point for the project's
core principle — *the LLM is the reasoning layer, not the prediction engine* —
by construction rather than by convention:

**What the LLM may emit**
    Categorical evidence types drawn from the existing Phase 7
    (:class:`EvidenceType`, :class:`AvailabilityStatus`) and Phase 8
    (:class:`TacticalEvidenceType`) taxonomies, a self-assessed confidence, the
    verbatim span of text that supports the claim, and its reasoning.

**What the LLM may not emit**
    * *Any timestamp.* There is no timestamp field in any extraction model.
      Temporal fields are inherited from the immutable ledger row, so the LLM
      is structurally unable to date-shift evidence.
    * *Any point projection, expected minutes, price or optimiser input.* No
      such field exists in the analyst output either; the analyst may only
      state a qualitative direction and magnitude.
    * *Any unknown key.* All models use ``extra="forbid"``, so a hallucinated
      field fails validation instead of being silently dropped.

**Grounding**
    Every extracted item must carry a ``source_quote`` that is a literal
    substring of the ledger row's text (whitespace-normalised, case-folded).
    :func:`quote_is_grounded` is the check; ungrounded items are rejected with
    a reason and counted, never quietly kept.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fpl_intelligence.availability.models import AvailabilityStatus, EvidenceType
from fpl_intelligence.live_intelligence.models import (
    TacticalDirection,
    TacticalEvidenceType,
)
from fpl_intelligence.live_intelligence.temporal_ledger import normalize_text

#: Bumped whenever the extraction contract changes. Persisted on every
#: extraction run so old rows remain interpretable.
EXTRACTION_SCHEMA_VERSION: Literal["phase9.extraction.v1"] = "phase9.extraction.v1"

#: Bumped whenever the analyst contract changes.
ANALYST_SCHEMA_VERSION: Literal["phase9.analyst.v1"] = "phase9.analyst.v1"

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class _StrictModel(BaseModel):
    """Base for every LLM-facing model: frozen, no unknown keys, no coercion."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Extraction contract
# ---------------------------------------------------------------------------


class ExtractedAvailabilityEvidence(_StrictModel):
    """One availability claim the LLM found in the text.

    Maps onto the existing Phase 7 ``availability_evidence`` table. Entity
    fields are *hints*, not ids: Phase 7's blockage was caused by a provider-key
    mismatch during resolution, so the LLM states the name exactly as written
    and resolution happens later, auditably, in the engine.
    """

    player_name: str = Field(min_length=1, max_length=200)
    team_name: str | None = Field(default=None, max_length=200)
    evidence_type: EvidenceType
    status_mentioned: AvailabilityStatus
    confidence: Confidence = 0.5
    #: Number of gameweeks the source says the player is expected to miss,
    #: only when explicitly stated. Never inferred.
    expected_absence_gameweeks: int | None = Field(default=None, ge=0, le=60)
    source_quote: str = Field(min_length=1, max_length=2000)
    reasoning: str = Field(default="", max_length=2000)

    @field_validator("player_name", "source_quote")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ExtractedTacticalEvidence(_StrictModel):
    """One tactical claim the LLM found in the text.

    Maps onto the new Phase 9 ``tactical_evidence`` table and the 15-signal
    Phase 8 taxonomy in ``docs/phase8-scope.md`` §3.
    """

    evidence_type: TacticalEvidenceType
    team_name: str | None = Field(default=None, max_length=200)
    player_name: str | None = Field(default=None, max_length=200)
    #: The signal value as written, e.g. ``"4-2-3-1"`` or the set-piece taker.
    value_text: str | None = Field(default=None, max_length=300)
    numeric_value: float | None = None
    direction: TacticalDirection = TacticalDirection.UNKNOWN
    confidence: Confidence = 0.5
    source_quote: str = Field(min_length=1, max_length=2000)
    reasoning: str = Field(default="", max_length=2000)

    @field_validator("source_quote")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @property
    def subject_hint(self) -> str | None:
        """The entity this signal is about, player taking precedence."""
        return self.player_name or self.team_name


class ExtractionEnvelope(_StrictModel):
    """The complete, strictly-typed JSON document the extractor must return.

    An empty extraction is a first-class, valid answer. Forcing the model to
    set ``no_evidence_found`` explicitly rather than returning empty arrays by
    accident distinguishes "the text said nothing" from "the call failed".
    """

    schema_version: Literal["phase9.extraction.v1"] = EXTRACTION_SCHEMA_VERSION
    availability_evidence: list[ExtractedAvailabilityEvidence] = Field(default_factory=list)
    tactical_evidence: list[ExtractedTacticalEvidence] = Field(default_factory=list)
    no_evidence_found: bool = False
    extraction_notes: str = Field(default="", max_length=4000)

    @property
    def total_items(self) -> int:
        return len(self.availability_evidence) + len(self.tactical_evidence)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Analyst contract
# ---------------------------------------------------------------------------


class QuantitativeCitation(_StrictModel):
    """The analyst restating the quantitative baseline it was given.

    The values are checked against the numbers supplied by
    :class:`~fpl_intelligence.optimization.provider.PlayerPrediction`. A
    mismatch is a guardrail failure, not a rounding quirk: it means the
    reasoning layer tried to become the prediction layer.
    """

    subject_ref: str = Field(min_length=1, max_length=100)
    expected_points: float
    start_probability: float
    floor: float
    ceiling: float
    interpretation: str = Field(default="", max_length=1500)


class QualitativeAdjustment(_StrictModel):
    """The analyst's qualitative overlay on top of the quantitative baseline.

    Deliberately has no numeric output. The analyst may say a signal points
    *down* and is *moderate*; it may never say "so it's really 5.2 points".
    Converting qualitative evidence into numbers is the job of the Phase 7
    availability adjustment path, which is quantitative and testable.
    """

    direction: Literal["up", "down", "neutral"] = "neutral"
    magnitude: Literal["none", "low", "moderate", "high"] = "none"
    #: Evidence refs, each of which must exist in the supplied bundle.
    cited_evidence_refs: list[str] = Field(default_factory=list)
    rationale: str = Field(default="", max_length=3000)


class AnalystOutput(_StrictModel):
    """The strictly-typed narrative the AI Analyst must return.

    Note the shape: the quantitative baseline and the qualitative adjustment
    are *separate, mandatory* fields. The analyst cannot blur them into a
    single number, which is exactly the separation the project requires.
    """

    schema_version: Literal["phase9.analyst.v1"] = ANALYST_SCHEMA_VERSION
    task: Literal["transfer_recommendation", "captaincy_debate", "differential_risk"]
    headline: str = Field(min_length=1, max_length=300)
    quantitative_baseline: list[QuantitativeCitation] = Field(min_length=1)
    qualitative_adjustment: QualitativeAdjustment
    net_assessment: str = Field(default="", max_length=4000)
    recommendation: Literal["proceed", "hold", "monitor", "avoid", "no_recommendation"]
    confidence: Confidence = 0.5
    caveats: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def quote_is_grounded(quote: str, raw_text: str) -> bool:
    """Return True when ``quote`` literally occurs in ``raw_text``.

    Comparison is whitespace-normalised and case-folded so that trivial
    formatting differences do not reject a genuine quote, but no fuzzy or
    semantic matching is performed: a paraphrase is a hallucination for these
    purposes and must be rejected.
    """
    if not quote or not raw_text:
        return False
    return normalize_text(quote).casefold() in normalize_text(raw_text).casefold()


#: Machine-readable JSON Schema for the extraction envelope, embedded verbatim
#: into the prompt so the model is told the contract rather than guessing it.
def extraction_json_schema() -> dict[str, Any]:
    """Return the JSON Schema of :class:`ExtractionEnvelope`."""
    return ExtractionEnvelope.model_json_schema()


def analyst_json_schema() -> dict[str, Any]:
    """Return the JSON Schema of :class:`AnalystOutput`."""
    return AnalystOutput.model_json_schema()
