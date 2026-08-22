"""Phase 9.3 — User-facing IntelligenceReport and PredictionContext.

The ``IntelligentReport`` is the final, presentation-ready document the AI Analyst
produces. It is deliberately separate from the internally-validated
:class:`~fpl_intelligence.live_intelligence.analyst.AnalystReport` so that the
synthesis schema (what a human reads) and the guardrail schema (what the engine
audits) can evolve independently.

Design rules
------------

* The report **never re-emits a revised projection.** It cites the quantitative
  baseline it was given via :class:`PredictionContext` but may only restate those
  numbers verbatim; it expresses its own view through
  ``recommendation`` (a category) and ``confidence`` (a self-assessed score),
  never through a new expected-points figure.

* ``unresolved_warnings`` surfaces evidence the analyst could not resolve to a
  canonical player at synthesis time, so a reader never sees a citation to a
  player they cannot act on.

* ``render_markdown()`` is a pure function of the model fields — no external
  I/O, no live lookups — so the same report always renders identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReportConfidence(StrEnum):
    """Coarse self-assessed confidence band for a rendered report."""

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


@dataclass(frozen=True)
class PredictionContext:
    """Read-only snapshot of the quantitative prediction the analyst must cite.

    Built from :class:`~fpl_intelligence.optimization.provider.PlayerPrediction`
    but deliberately minimal: only the fields the analyst is permitted to
    restate. No mutable projection is ever accepted here.
    """

    player_id: int
    gameweek: int
    expected_points: float
    expected_minutes: float
    start_probability: float
    floor: float
    ceiling: float
    model_confidence: float = 1.0
    fixture_count: int = 1
    subject_ref: str = ""
    display_name: str | None = None

    def to_prompt_dict(self) -> dict[str, Any]:
        ref = self.subject_ref or f"player:{self.player_id}"
        return {
            "subject_ref": ref,
            "display_name": self.display_name or ref,
            "player_id": self.player_id,
            "gameweek": self.gameweek,
            "expected_points": self.expected_points,
            "expected_minutes": self.expected_minutes,
            "start_probability": self.start_probability,
            "floor": self.floor,
            "ceiling": self.ceiling,
            "fixture_count": self.fixture_count,
        }


@dataclass(frozen=True)
class ReportEvidenceCitation:
    """A single evidence citation embedded in the user-facing report."""

    evidence_ref: str
    kind: str
    subject_ref: str | None
    summary: str
    source_name: str
    source_reliability: str
    confidence: float
    direction: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_ref": self.evidence_ref,
            "kind": self.kind,
            "subject_ref": self.subject_ref,
            "summary": self.summary,
            "source_name": self.source_name,
            "source_reliability": self.source_reliability,
            "confidence": round(self.confidence, 4),
            "direction": self.direction,
        }


@dataclass(frozen=True)
class ReportQuantitativeBaseline:
    """The quantitative baseline the analyst was given, restated for the reader."""

    subject_ref: str
    player_id: int
    gameweek: int
    expected_points: float
    expected_minutes: float
    start_probability: float
    floor: float
    ceiling: float
    fixture_count: int
    display_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_ref": self.subject_ref,
            "player_id": self.player_id,
            "gameweek": self.gameweek,
            "expected_points": self.expected_points,
            "expected_minutes": self.expected_minutes,
            "start_probability": self.start_probability,
            "floor": self.floor,
            "ceiling": self.ceiling,
            "fixture_count": self.fixture_count,
            "display_name": self.display_name,
        }


@dataclass(frozen=True)
class ReportQualitativeAdjustment:
    """The analyst's own qualitative overlay — direction + magnitude, no numbers."""

    direction: str
    magnitude: str
    cited_evidence_refs: list[str]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "magnitude": self.magnitude,
            "cited_evidence_refs": list(self.cited_evidence_refs),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class UnresolvedWarning:
    """A piece of evidence that could not be resolved to a canonical player."""

    evidence_ref: str
    kind: str
    subject_hint: str | None
    resolution_status: str
    resolution_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_ref": self.evidence_ref,
            "kind": self.kind,
            "subject_hint": self.subject_hint,
            "resolution_status": self.resolution_status,
            "resolution_reason": self.resolution_reason,
        }


class IntelligenceReport(BaseModel):
    """The user-facing synthesis of quantitative prediction + qualitative evidence.

    This is the presentation layer. Unlike the internally-validated
    ``AnalystReport``, it is optimised for readability and downstream
    consumption (e.g. by a CLI ``--analyst`` dry-run or a Markdown report writer),
    not for guardrail enforcement — those checks happened before this object was
    built.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "phase9.report.v1"
    task: str
    headline: str
    prediction_context: ReportQuantitativeBaseline
    qualitative_adjustment: ReportQualitativeAdjustment
    net_assessment: str = ""
    recommendation: str
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_band: str = ReportConfidence.LOW
    citations: list[ReportEvidenceCitation] = Field(default_factory=list)
    unresolved_warnings: list[UnresolvedWarning] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    generated_at: datetime | None = None
    provider_name: str = ""
    model_name: str = ""
    is_mock: bool = False
    prompt_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task": self.task,
            "headline": self.headline,
            "prediction_context": self.prediction_context.to_dict(),
            "qualitative_adjustment": self.qualitative_adjustment.to_dict(),
            "net_assessment": self.net_assessment,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "confidence_band": self.confidence_band,
            "citations": [c.to_dict() for c in self.citations],
            "unresolved_warnings": [w.to_dict() for w in self.unresolved_warnings],
            "caveats": list(self.caveats),
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "is_mock": self.is_mock,
            "prompt_hash": self.prompt_hash,
        }

    def render_markdown(self) -> str:
        """Render the report as a human-readable Markdown document.

        Pure function: depends only on model fields, no external I/O.
        """
        lines: list[str] = []
        pc = self.prediction_context
        label = pc.display_name or pc.subject_ref

        lines.append(f"# {self.headline}")
        lines.append("")
        lines.append(f"**Task:** {self.task}  ")
        lines.append(f"**Recommendation:** {self.recommendation}  ")
        lines.append(f"**Confidence:** {self.confidence:.2f} ({self.confidence_band})")
        if self.provider_name or self.model_name:
            mock_tag = " *(mock)*" if self.is_mock else ""
            provider = self.provider_name or "unknown"
            model = self.model_name or "unknown"
            lines.append(f"**Provider:** {provider} / {model}{mock_tag}")
        lines.append("")
        if self.generated_at:
            lines.append(f"_Generated: {self.generated_at.isoformat()}_  ")
            lines.append("")

        lines.append("## Quantitative Baseline")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Player | {label} ({pc.subject_ref}) |")
        lines.append(f"| Gameweek | {pc.gameweek} |")
        lines.append(f"| Fixtures | {pc.fixture_count} |")
        lines.append(f"| Expected points | {pc.expected_points} |")
        lines.append(f"| Expected minutes | {pc.expected_minutes} |")
        lines.append(f"| Start probability | {pc.start_probability:.2%} |")
        lines.append(f"| Floor (P10) | {pc.floor} |")
        lines.append(f"| Ceiling (P90) | {pc.ceiling} |")
        lines.append("")

        lines.append("## Qualitative Assessment")
        lines.append("")
        adj = self.qualitative_adjustment
        lines.append(f"**Direction:** {adj.direction}  ")
        lines.append(f"**Magnitude:** {adj.magnitude}  ")
        if adj.cited_evidence_refs:
            refs = ", ".join(f"`{r}`" for r in adj.cited_evidence_refs)
            lines.append(f"**Evidence refs:** {refs}  ")
        lines.append("")
        if adj.rationale:
            lines.append(f"> {adj.rationale}")
            lines.append("")

        if self.citations:
            lines.append("## Evidence Cited")
            lines.append("")
            lines.append("| Ref | Kind | Summary | Source | Reliability | Confidence |")
            lines.append("|-----|------|---------|--------|-------------|------------|")
            for c in self.citations:
                summary = c.summary.replace("|", "\\|")[:80]
                lines.append(
                    f"| `{c.evidence_ref}` | {c.kind} | {summary} "
                    f"| {c.source_name} | {c.source_reliability} "
                    f"| {c.confidence:.2f} |"
                )
            lines.append("")

        if self.unresolved_warnings:
            lines.append("## Unresolved Warnings")
            lines.append("")
            for w in self.unresolved_warnings:
                hint = w.subject_hint or "(unnamed)"
                lines.append(
                    f"- `{w.evidence_ref}` ({w.kind}): **{hint}** — "
                    f"{w.resolution_status}: {w.resolution_reason}"
                )
            lines.append("")

        if self.net_assessment:
            lines.append("## Net Assessment")
            lines.append("")
            lines.append(self.net_assessment)
            lines.append("")

        if self.caveats:
            lines.append("## Caveats")
            lines.append("")
            for caveat in self.caveats:
                lines.append(f"- {caveat}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"
