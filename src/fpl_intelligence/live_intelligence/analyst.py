"""Phase 9 AI Analyst — narrative synthesis over quant + qual.

The analyst is the only place in the system where a language model touches
decision-facing output, so it is the place where the project's core principle
must be *enforced*, not merely stated:

    "LLM/AI is the reasoning layer, not the core prediction engine."

Four enforcement barriers
-------------------------

1. **Read-only quantitative input.** The analyst consumes
   :class:`~fpl_intelligence.optimization.provider.PlayerPrediction` through the
   existing :class:`DecisionPredictionProvider` interface. It never constructs,
   fits, mutates or re-weights a prediction, and nothing it returns is fed back
   into the optimiser.

2. **No numeric output channel.** :class:`AnalystOutput` has no field for a
   revised projection. The analyst's only quantitative field is a *restatement*
   of what it was given.

3. **Restatement verification.** :meth:`AIAnalyst.analyse` compares the model's
   restated numbers against the supplied baseline and raises
   :class:`AnalystGuardrailError` on any drift. A model that "corrects" the
   engine fails loudly.

4. **Pre-deadline evidence filter.** Evidence is filtered against the gameweek
   deadline *before* prompting, using the same
   :class:`InformationAccessPolicy` as the rest of the engine. Post-deadline
   evidence never reaches the prompt, so the analyst cannot leak it into a
   narrative even if it wanted to.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from fpl_intelligence.features.temporal import InformationAccessPolicy
from fpl_intelligence.live_intelligence.analyst_prompts import (
    CAPTAINCY_DEBATE,
    DIFFERENTIAL_RISK,
    TRANSFER_RECOMMENDATION,
)
from fpl_intelligence.live_intelligence.extraction import (
    LLMProvider,
    LLMProviderError,
    strip_code_fence,
)
from fpl_intelligence.live_intelligence.models import LedgerTemporalClass
from fpl_intelligence.live_intelligence.prompts import LLMPrompt, PromptTemplate
from fpl_intelligence.live_intelligence.schemas import ANALYST_SCHEMA_VERSION, AnalystOutput
from fpl_intelligence.live_intelligence.temporal_ledger import Clock, utc_now
from fpl_intelligence.optimization.provider import (
    DecisionPredictionProvider,
    PlayerPrediction,
)

#: Tolerance when verifying that the analyst restated the baseline unchanged.
#: Tight enough that any real "adjustment" trips it, loose enough to survive
#: float round-tripping through JSON.
RESTATEMENT_TOLERANCE = 1e-4


class AnalystGuardrailError(RuntimeError):
    """Raised when the analyst's output violates the separation of layers.

    Deliberately not recoverable-by-default. A model that altered the
    quantitative baseline, cited evidence it was not given, or manufactured a
    qualitative adjustment out of an empty bundle has produced output that must
    not be shown to a user or persisted as reasoning.
    """


class LeakageError(RuntimeError):
    """Raised when evidence postdating the deadline is passed in strict mode."""


class AnalystTask(StrEnum):
    """The three synthesis tasks Phase 9 supports."""

    TRANSFER_RECOMMENDATION = "transfer_recommendation"
    CAPTAINCY_DEBATE = "captaincy_debate"
    DIFFERENTIAL_RISK = "differential_risk"


_TASK_TEMPLATES: dict[str, PromptTemplate] = {
    AnalystTask.TRANSFER_RECOMMENDATION: TRANSFER_RECOMMENDATION,
    AnalystTask.CAPTAINCY_DEBATE: CAPTAINCY_DEBATE,
    AnalystTask.DIFFERENTIAL_RISK: DIFFERENTIAL_RISK,
}


# ---------------------------------------------------------------------------
# Context objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuantitativeBaseline:
    """A read-only snapshot of one Phase 4/5/6 prediction.

    Built from :class:`PlayerPrediction` and never modified. ``subject_ref`` is
    the stable handle the analyst must cite.
    """

    subject_ref: str
    player_id: int
    gameweek: int
    expected_points: float
    expected_minutes: float
    start_probability: float
    floor: float
    ceiling: float
    model_confidence: float = 1.0
    fixture_count: int = 1
    display_name: str | None = None

    @classmethod
    def from_prediction(
        cls,
        prediction: PlayerPrediction,
        *,
        subject_ref: str | None = None,
        fixture_count: int = 1,
        display_name: str | None = None,
    ) -> QuantitativeBaseline:
        """Project a :class:`PlayerPrediction` into an analyst baseline."""
        return cls(
            subject_ref=subject_ref or f"player:{prediction.player_id}",
            player_id=prediction.player_id,
            gameweek=prediction.gameweek,
            expected_points=round(float(prediction.expected_points), 4),
            expected_minutes=round(float(prediction.expected_minutes), 4),
            start_probability=round(float(prediction.start_probability), 4),
            floor=round(float(prediction.floor), 4),
            ceiling=round(float(prediction.ceiling), 4),
            model_confidence=float(prediction.confidence),
            fixture_count=fixture_count,
            display_name=display_name,
        )

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "subject_ref": self.subject_ref,
            "display_name": self.display_name or self.subject_ref,
            "gameweek": self.gameweek,
            "expected_points": self.expected_points,
            "expected_minutes": self.expected_minutes,
            "start_probability": self.start_probability,
            "floor": self.floor,
            "ceiling": self.ceiling,
            "fixture_count": self.fixture_count,
        }


@dataclass(frozen=True)
class EvidenceCitation:
    """One piece of Phase 7/8 qualitative evidence offered to the analyst."""

    evidence_ref: str
    kind: str  # "availability" | "tactical"
    summary: str
    source_name: str
    source_reliability: str
    confidence: float
    available_at: datetime
    ingested_at: datetime
    temporal_class: str
    direction: str = "unknown"
    subject_ref: str | None = None
    source_quote: str | None = None
    is_mock: bool = False

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "evidence_ref": self.evidence_ref,
            "kind": self.kind,
            "subject_ref": self.subject_ref,
            "summary": self.summary,
            "direction": self.direction,
            "source": self.source_name,
            "reliability": self.source_reliability,
            "confidence": round(self.confidence, 4),
            "quote": self.source_quote,
        }


@dataclass(frozen=True)
class AnalystContext:
    """Everything the analyst is allowed to see for one question."""

    task: AnalystTask
    subject_label: str
    gameweek: int
    deadline: datetime | None
    baselines: list[QuantitativeBaseline]
    evidence: list[EvidenceCitation] = field(default_factory=list)
    notes: str = ""

    def baseline_refs(self) -> set[str]:
        return {b.subject_ref for b in self.baselines}

    def evidence_refs(self) -> set[str]:
        return {e.evidence_ref for e in self.evidence}


@dataclass(frozen=True)
class AnalystReport:
    """Validated analyst output plus the provenance needed to audit it."""

    context: AnalystContext
    output: AnalystOutput
    provider_name: str
    model_name: str
    is_mock: bool
    prompt_hash: str
    template_id: str
    template_version: str
    schema_version: str
    generated_at: datetime
    excluded_evidence: list[dict[str, Any]] = field(default_factory=list)
    raw_response: str | None = None

    @property
    def cites_quantitative_baseline(self) -> bool:
        """True when every supplied subject was restated by the analyst."""
        cited = {c.subject_ref for c in self.output.quantitative_baseline}
        return self.context.baseline_refs().issubset(cited)

    @property
    def cites_qualitative_evidence(self) -> bool:
        """True when the adjustment cites at least one supplied evidence ref."""
        return bool(self.output.qualitative_adjustment.cited_evidence_refs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": str(self.context.task),
            "subject_label": self.context.subject_label,
            "gameweek": self.context.gameweek,
            "output": self.output.to_dict(),
            "provider": self.provider_name,
            "model": self.model_name,
            "is_mock": self.is_mock,
            "prompt_hash": self.prompt_hash,
            "template_id": self.template_id,
            "schema_version": self.schema_version,
            "generated_at": self.generated_at.isoformat(),
            "excluded_evidence": self.excluded_evidence,
            "cites_quantitative_baseline": self.cites_quantitative_baseline,
            "cites_qualitative_evidence": self.cites_qualitative_evidence,
        }


# ---------------------------------------------------------------------------
# The analyst
# ---------------------------------------------------------------------------


class AIAnalyst:
    """Synthesises quantitative predictions and qualitative evidence into prose.

    Args:
        provider: Any :class:`LLMProvider`, real or mock.
        prediction_provider: Optional Phase 4/5/6 provider. When supplied, the
            convenience methods pull baselines from it directly; it is used
            strictly read-only.
        policy: Information-access policy for the pre-deadline evidence filter.
        clock: Injected clock for deterministic ``generated_at``.
        strict_leakage: When True (the default) passing post-deadline evidence
            raises :class:`LeakageError`. When False such evidence is silently
            *excluded* and reported in ``AnalystReport.excluded_evidence`` —
            never included.
        allow_mock_evidence: When False (the default) evidence produced by a
            mock extraction is excluded, so scaffold artefacts cannot appear in
            a narrative that reads as though it were real.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        prediction_provider: DecisionPredictionProvider | None = None,
        policy: InformationAccessPolicy = InformationAccessPolicy.STRICT_REPRODUCIBILITY,
        clock: Clock = utc_now,
        strict_leakage: bool = True,
        allow_mock_evidence: bool = False,
    ) -> None:
        self._provider = provider
        self._predictions = prediction_provider
        self._policy = policy
        self._clock = clock
        self._strict_leakage = strict_leakage
        self._allow_mock_evidence = allow_mock_evidence

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    # -- baseline construction --------------------------------------------

    def baseline_for(
        self,
        player_id: int,
        gameweek: int,
        *,
        subject_ref: str | None = None,
        display_name: str | None = None,
    ) -> QuantitativeBaseline:
        """Read one baseline from the Phase 4/5/6 provider.

        Read-only: the prediction is projected into an immutable snapshot and
        the provider is never asked to change anything.
        """
        if self._predictions is None:
            raise ValueError(
                "No DecisionPredictionProvider was supplied; pass baselines "
                "explicitly or construct AIAnalyst with prediction_provider."
            )
        prediction = self._predictions.get_player_prediction(player_id, gameweek)
        fixtures = self._predictions.get_fixture_count(player_id, gameweek)
        return QuantitativeBaseline.from_prediction(
            prediction,
            subject_ref=subject_ref,
            fixture_count=fixtures,
            display_name=display_name,
        )

    # -- public tasks ------------------------------------------------------

    def transfer_recommendation(
        self,
        baselines: list[QuantitativeBaseline],
        evidence: list[EvidenceCitation],
        *,
        subject_label: str,
        gameweek: int,
        deadline: datetime | None,
        notes: str = "",
    ) -> AnalystReport:
        """Reason about a transfer, citing baseline against evidence."""
        return self.analyse(
            AnalystContext(
                task=AnalystTask.TRANSFER_RECOMMENDATION,
                subject_label=subject_label,
                gameweek=gameweek,
                deadline=deadline,
                baselines=baselines,
                evidence=evidence,
                notes=notes,
            )
        )

    def captaincy_debate(
        self,
        baselines: list[QuantitativeBaseline],
        evidence: list[EvidenceCitation],
        *,
        gameweek: int,
        deadline: datetime | None,
        subject_label: str | None = None,
        notes: str = "",
    ) -> AnalystReport:
        """Summarise a captaincy debate across two or more candidates."""
        if len(baselines) < 2:
            raise ValueError(
                "A captaincy debate needs at least two candidates; "
                f"got {len(baselines)}."
            )
        label = subject_label or " vs ".join(
            b.display_name or b.subject_ref for b in baselines
        )
        return self.analyse(
            AnalystContext(
                task=AnalystTask.CAPTAINCY_DEBATE,
                subject_label=label,
                gameweek=gameweek,
                deadline=deadline,
                baselines=baselines,
                evidence=evidence,
                notes=notes,
            )
        )

    def differential_risk(
        self,
        baseline: QuantitativeBaseline,
        evidence: list[EvidenceCitation],
        *,
        gameweek: int,
        deadline: datetime | None,
        subject_label: str | None = None,
        notes: str = "",
    ) -> AnalystReport:
        """Profile the risk of a low-ownership differential pick."""
        return self.analyse(
            AnalystContext(
                task=AnalystTask.DIFFERENTIAL_RISK,
                subject_label=subject_label or baseline.display_name or baseline.subject_ref,
                gameweek=gameweek,
                deadline=deadline,
                baselines=[baseline],
                evidence=evidence,
                notes=notes,
            )
        )

    # -- core --------------------------------------------------------------

    def analyse(self, context: AnalystContext) -> AnalystReport:
        """Run one analyst task end-to-end with all guardrails applied."""
        if not context.baselines:
            raise ValueError(
                "AnalystContext requires at least one quantitative baseline: the "
                "analyst may only reason relative to the quantitative engine, "
                "never in place of it."
            )

        safe_evidence, excluded = self._filter_evidence(context)
        safe_context = AnalystContext(
            task=context.task,
            subject_label=context.subject_label,
            gameweek=context.gameweek,
            deadline=context.deadline,
            baselines=context.baselines,
            evidence=safe_evidence,
            notes=context.notes,
        )

        prompt = self.build_prompt(safe_context)
        try:
            response = self._provider.complete(prompt)
        except LLMProviderError as exc:
            raise AnalystGuardrailError(f"analyst provider call failed: {exc}") from exc

        try:
            payload = json.loads(strip_code_fence(response.text))
        except (json.JSONDecodeError, TypeError) as exc:
            raise AnalystGuardrailError(
                f"analyst response is not valid JSON: {exc}"
            ) from exc

        try:
            output = AnalystOutput.model_validate(payload)
        except ValidationError as exc:
            raise AnalystGuardrailError(
                "analyst response failed schema validation: "
                f"{exc.errors(include_url=False)[:3]}"
            ) from exc

        self._enforce_guardrails(safe_context, output)

        return AnalystReport(
            context=safe_context,
            output=output,
            provider_name=response.provider_name,
            model_name=response.model_name,
            is_mock=response.is_mock,
            prompt_hash=prompt.hash(),
            template_id=prompt.template_id,
            template_version=prompt.version,
            schema_version=ANALYST_SCHEMA_VERSION,
            generated_at=self._clock(),
            excluded_evidence=excluded,
            raw_response=response.text,
        )

    def build_prompt(self, context: AnalystContext) -> LLMPrompt:
        """Render the analyst prompt for a (already-filtered) context."""
        template = _TASK_TEMPLATES[context.task]
        baseline_dicts = [b.to_prompt_dict() for b in context.baselines]
        evidence_dicts = [e.to_prompt_dict() for e in context.evidence]

        return template.render(
            context={
                "task": str(context.task),
                "subject_label": context.subject_label,
                "gameweek": context.gameweek,
                "baselines": baseline_dicts,
                "evidence": evidence_dicts,
            },
            gameweek=context.gameweek,
            deadline=context.deadline.isoformat() if context.deadline else "unknown",
            subject_label=context.subject_label,
            baseline_block=json.dumps(baseline_dicts, indent=2, sort_keys=True),
            evidence_block=(
                json.dumps(evidence_dicts, indent=2, sort_keys=True)
                if evidence_dicts
                else "(none — no pre-deadline qualitative evidence is available)"
            ),
        )

    # -- guardrails --------------------------------------------------------

    def _filter_evidence(
        self, context: AnalystContext
    ) -> tuple[list[EvidenceCitation], list[dict[str, Any]]]:
        """Drop anything that is not real, pre-deadline qualitative evidence."""
        kept: list[EvidenceCitation] = []
        excluded: list[dict[str, Any]] = []

        for item in context.evidence:
            reason = self._exclusion_reason(item, context.deadline)
            if reason is None:
                kept.append(item)
                continue
            record = {"evidence_ref": item.evidence_ref, "reason": reason}
            if self._strict_leakage and reason.startswith("post-deadline"):
                raise LeakageError(
                    f"Evidence '{item.evidence_ref}' {reason}. Passing "
                    "post-deadline evidence to the analyst is a look-ahead "
                    "leak; filter it upstream or set strict_leakage=False to "
                    "have it excluded and reported instead."
                )
            excluded.append(record)

        return kept, excluded

    def _exclusion_reason(
        self, item: EvidenceCitation, deadline: datetime | None
    ) -> str | None:
        if item.is_mock and not self._allow_mock_evidence:
            return "was produced by a mock extraction and is not real evidence"
        if item.temporal_class == LedgerTemporalClass.NO_DEADLINE_CONTEXT:
            return "has no resolved deadline context, so its eligibility is undecided"
        if deadline is None:
            return "cannot be checked: the context has no deadline"

        available_ok = item.available_at <= deadline
        ingested_ok = item.ingested_at <= deadline
        if self._policy == InformationAccessPolicy.PUBLIC_AVAILABILITY:
            ok = available_ok
        elif self._policy == InformationAccessPolicy.SYSTEM_AVAILABILITY:
            ok = ingested_ok
        else:
            ok = available_ok and ingested_ok

        if not ok:
            return (
                f"post-deadline under {self._policy}: available_at="
                f"{item.available_at.isoformat()}, ingested_at="
                f"{item.ingested_at.isoformat()}, deadline={deadline.isoformat()}"
            )
        return None

    def _enforce_guardrails(self, context: AnalystContext, output: AnalystOutput) -> None:
        """Reject output that blurs the quantitative/qualitative boundary."""
        if str(output.task) != str(context.task):
            raise AnalystGuardrailError(
                f"analyst returned task '{output.task}' but was asked for "
                f"'{context.task}'."
            )

        # 1. Every supplied subject must be restated.
        supplied = {b.subject_ref: b for b in context.baselines}
        restated = {c.subject_ref: c for c in output.quantitative_baseline}
        missing = set(supplied) - set(restated)
        if missing:
            raise AnalystGuardrailError(
                "analyst failed to cite the quantitative baseline for: "
                f"{sorted(missing)}. Every subject must be restated explicitly."
            )
        invented = set(restated) - set(supplied)
        if invented:
            raise AnalystGuardrailError(
                f"analyst cited baselines that were never supplied: {sorted(invented)}."
            )

        # 2. The restated numbers must be unchanged.
        for ref, baseline in supplied.items():
            citation = restated[ref]
            for label, given, echoed in (
                ("expected_points", baseline.expected_points, citation.expected_points),
                (
                    "start_probability",
                    baseline.start_probability,
                    citation.start_probability,
                ),
                ("floor", baseline.floor, citation.floor),
                ("ceiling", baseline.ceiling, citation.ceiling),
            ):
                if abs(float(given) - float(echoed)) > RESTATEMENT_TOLERANCE:
                    raise AnalystGuardrailError(
                        f"analyst altered the quantitative baseline for '{ref}': "
                        f"{label} was {given} but was restated as {echoed}. The "
                        "reasoning layer may not modify engine output."
                    )

        # 3. Citations must resolve to evidence that was actually supplied.
        adjustment = output.qualitative_adjustment
        available_refs = context.evidence_refs()
        unknown = set(adjustment.cited_evidence_refs) - available_refs
        if unknown:
            raise AnalystGuardrailError(
                f"analyst cited evidence that was never supplied: {sorted(unknown)}. "
                "This is a hallucinated citation."
            )

        # 4. No evidence means no adjustment.
        if not available_refs:
            if adjustment.direction != "neutral" or adjustment.magnitude != "none":
                raise AnalystGuardrailError(
                    "analyst asserted a qualitative adjustment "
                    f"(direction={adjustment.direction}, "
                    f"magnitude={adjustment.magnitude}) with no evidence supplied."
                )
        elif adjustment.direction != "neutral" and not adjustment.cited_evidence_refs:
            raise AnalystGuardrailError(
                f"analyst asserted a '{adjustment.direction}' adjustment without "
                "citing any evidence ref."
            )
