"""Phase 9 extraction prompt templates.

Templates are versioned, hashable objects rather than f-strings scattered
through the code. The hash of the fully rendered prompt is persisted on every
:class:`~fpl_intelligence.live_intelligence.models.LLMExtractionRun`, so an
extraction can be reproduced exactly, and a silent prompt change shows up as a
hash change in the audit trail.

Three rules are repeated in every template because they are the ones that keep
the reasoning layer inside its lane:

1. **Quote verbatim.** Every claim must carry a literal span from the text.
   The engine re-checks this and discards anything that fails.
2. **Never date anything.** The model is told explicitly that timestamps are
   supplied by the ledger and that emitting one is a contract violation.
3. **Never infer beyond the text.** Absence of a statement is not evidence of
   absence; the model returns an empty result rather than a plausible guess.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fpl_intelligence.availability.models import AvailabilityStatus, EvidenceType
from fpl_intelligence.live_intelligence.models import (
    TacticalDirection,
    TacticalEvidenceType,
)
from fpl_intelligence.live_intelligence.schemas import EXTRACTION_SCHEMA_VERSION


@dataclass(frozen=True)
class LLMPrompt:
    """A fully rendered prompt, ready to hand to a provider.

    ``context`` carries the same information as the rendered text in structured
    form. Real providers ignore it and send ``system`` + ``user``; the mock
    provider reads it so the test double stays deterministic without having to
    parse prose back out of the prompt.
    """

    template_id: str
    version: str
    system: str
    user: str
    schema_version: str
    context: dict[str, Any] = field(default_factory=dict)

    def hash(self) -> str:
        """SHA-256 over the exact bytes that would be sent to the model."""
        payload = f"{self.template_id}|{self.version}|{self.system}|{self.user}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptTemplate:
    """A versioned system/user template pair."""

    template_id: str
    version: str
    system: str
    user: str
    schema_version: str = EXTRACTION_SCHEMA_VERSION

    def render(self, *, context: dict[str, Any] | None = None, **values: Any) -> LLMPrompt:
        """Render the template with ``values`` substituted into the user block."""
        return LLMPrompt(
            template_id=self.template_id,
            version=self.version,
            system=self.system,
            user=self.user.format(**values),
            schema_version=self.schema_version,
            context=dict(context or {}),
        )


def _enum_list(enum_cls: type[Enum]) -> str:
    return ", ".join(f'"{m.value}"' for m in enum_cls)


_SHARED_RULES = f"""\
NON-NEGOTIABLE RULES

1. GROUND EVERY CLAIM. Each item you emit MUST include `source_quote`: a
   contiguous, verbatim span copied character-for-character from the SOURCE
   TEXT. Do not paraphrase, summarise, translate or reconstruct the quote. The
   receiving system re-checks that the quote literally occurs in the text and
   discards any item that fails, so a paraphrased quote destroys your own
   output.

2. EMIT NO TIMESTAMPS, EVER. Do not output dates, times, deadlines, gameweek
   numbers or any temporal field. The system already knows exactly when this
   text was published, captured and ingested, and attaches those values itself.
   Any temporal field you invent would corrupt a no-look-ahead backtest. There
   is no place in the schema for one; adding a key will fail validation.

3. DO NOT INFER BEYOND THE TEXT. Report only what the text states or directly
   and unambiguously implies. Silence is not evidence. If the text contains no
   relevant information, return empty arrays and set `no_evidence_found` to
   true. An honest empty result is correct and expected; a plausible invention
   is a failure.

4. EMIT NO PREDICTIONS. Do not output expected points, projected minutes,
   prices, ownership, ratings or any numeric forecast. A separate quantitative
   engine owns all forecasting. Your job is to convert text into typed facts.

5. RETURN ONE JSON OBJECT AND NOTHING ELSE. No markdown fences, no preamble,
   no trailing commentary. Unknown keys are rejected; omit fields you cannot
   fill rather than guessing a value.

6. CALIBRATE CONFIDENCE HONESTLY. `confidence` in [0.0, 1.0] reflects how
   unambiguously the text supports the claim, not how likely you think the
   outcome is. An explicit statement from a named manager is high; a hedged
   aside is low.

OUTPUT SCHEMA (schema_version = "{EXTRACTION_SCHEMA_VERSION}")

{{
  "schema_version": "{EXTRACTION_SCHEMA_VERSION}",
  "availability_evidence": [
    {{
      "player_name": "<name exactly as written in the text>",
      "team_name": "<team name or null>",
      "evidence_type": <one of: {_enum_list(EvidenceType)}>,
      "status_mentioned": <one of: {_enum_list(AvailabilityStatus)}>,
      "confidence": <float 0.0-1.0>,
      "expected_absence_gameweeks": <int or null; ONLY if explicitly stated>,
      "source_quote": "<verbatim span from the text>",
      "reasoning": "<one sentence on why the text supports this>"
    }}
  ],
  "tactical_evidence": [
    {{
      "evidence_type": <one of: {_enum_list(TacticalEvidenceType)}>,
      "team_name": "<team name or null>",
      "player_name": "<player name or null>",
      "value_text": "<the signal value, e.g. '4-3-3' or the taker's name>",
      "numeric_value": <float or null>,
      "direction": <one of: {_enum_list(TacticalDirection)}>,
      "confidence": <float 0.0-1.0>,
      "source_quote": "<verbatim span from the text>",
      "reasoning": "<one sentence on why the text supports this>"
    }}
  ],
  "no_evidence_found": <true only if BOTH arrays are empty>,
  "extraction_notes": "<optional: ambiguities you deliberately did not resolve>"
}}

AVAILABILITY STATUS SEMANTICS — pick ONE status_mentioned per availability item.
The allowed values are: {_enum_list(AvailabilityStatus)}

Normalise the source wording onto the canonical status; never invent a value.
Worked examples:
  "ruled out for four to six weeks"                        -> "out"
  "he is suspended for this fixture"                       -> "suspended"
  "it is touch and go, we will check Saturday morning"     -> "doubtful"
  "trained fully and he is available, but will he start?"  -> "available"
  "the manager confirmed he will start"                    -> "start"
  "back in training but not this weekend"                  -> "available" if the
     text implies he could be involved soon but not confirmed for this match;
     -> "bench" if the text says he will be on the bench
"available" means the player is reported fit/available/in contention, but not
explicitly confirmed to start.
Use "unknown" ONLY when no status can be inferred from the text at all — e.g.
the source states no availability-relevant fact. Never use "unknown" merely
because you are unsure which of the concrete statuses best fits; pick the most
defensible concrete one and be honest about it in `reasoning`.

"""


AVAILABILITY_EXTRACTION = PromptTemplate(
    template_id="phase9.extract.availability",
    version="1.1.0",
    system=(
        "You are a precise information-extraction component inside a Fantasy "
        "Premier League intelligence engine. You convert unstructured English "
        "football text into strictly-typed JSON evidence about PLAYER "
        "AVAILABILITY: injuries, illness, suspensions, fitness, training "
        "participation, manager statements and lineup hints.\n\n"
        "You are a reasoning and normalisation layer. You are NOT a predictor. "
        "A separate quantitative engine forecasts points and minutes; your "
        "output is consumed as evidence, not as a forecast.\n\n"
        + _SHARED_RULES
        + "\nEmit only `availability_evidence`. Leave `tactical_evidence` empty."
    ),
    user=(
        "SOURCE METADATA (context only — do not copy into your output)\n"
        "  source_name: {source_name}\n"
        "  source_type: {source_type}\n"
        "  source_reliability: {source_reliability}\n"
        "  team_context: {team_hint}\n\n"
        "SOURCE TEXT\n"
        "<<<BEGIN>>>\n"
        "{raw_text}\n"
        "<<<END>>>\n\n"
        "Extract all PLAYER AVAILABILITY evidence. Return one JSON object."
    ),
)


TACTICAL_EXTRACTION = PromptTemplate(
    template_id="phase9.extract.tactical",
    version="1.1.0",
    system=(
        "You are a precise information-extraction component inside a Fantasy "
        "Premier League intelligence engine. You convert unstructured English "
        "football text into strictly-typed JSON evidence about TACTICS: "
        "formations, starting-lineup hints, player positions and role changes, "
        "set-piece duties (penalties, free kicks, corners), manager changes and "
        "tendencies, rotation risk, team style, and opponent matchup context.\n\n"
        "You are a reasoning and normalisation layer. You are NOT a predictor. "
        "A separate quantitative engine forecasts points and minutes; your "
        "output is consumed as evidence, not as a forecast.\n\n"
        "DIRECTION SEMANTICS: `direction` describes the effect on the named "
        "player's FPL prospects — 'positive' (more minutes, better role, gains "
        "set-piece duty), 'negative' (rotation risk, demoted role, loses set-"
        "piece duty), 'neutral' (no clear effect), 'unknown' (the text does not "
        "say). Do not guess a direction to appear useful.\n\n"
        + _SHARED_RULES
        + "\nEmit only `tactical_evidence`. Leave `availability_evidence` empty."
    ),
    user=(
        "SOURCE METADATA (context only — do not copy into your output)\n"
        "  source_name: {source_name}\n"
        "  source_type: {source_type}\n"
        "  source_reliability: {source_reliability}\n"
        "  team_context: {team_hint}\n\n"
        "SOURCE TEXT\n"
        "<<<BEGIN>>>\n"
        "{raw_text}\n"
        "<<<END>>>\n\n"
        "Extract all TACTICAL evidence. Return one JSON object."
    ),
)


COMBINED_EXTRACTION = PromptTemplate(
    template_id="phase9.extract.combined",
    version="1.1.0",
    system=(
        "You are a precise information-extraction component inside a Fantasy "
        "Premier League intelligence engine. You convert unstructured English "
        "football text into strictly-typed JSON evidence of two kinds:\n\n"
        "  AVAILABILITY — injuries, illness, suspensions, fitness, training "
        "participation, manager statements, lineup hints.\n"
        "  TACTICAL — formations, lineup hints, positions and role changes, "
        "set-piece duties, manager changes and tendencies, rotation risk, team "
        "style, matchup context.\n\n"
        "A single sentence may yield both kinds; emit each separately with its "
        "own quote. You are a reasoning and normalisation layer, NOT a "
        "predictor.\n\n"
        + _SHARED_RULES
    ),
    user=(
        "SOURCE METADATA (context only — do not copy into your output)\n"
        "  source_name: {source_name}\n"
        "  source_type: {source_type}\n"
        "  source_reliability: {source_reliability}\n"
        "  team_context: {team_hint}\n\n"
        "SOURCE TEXT\n"
        "<<<BEGIN>>>\n"
        "{raw_text}\n"
        "<<<END>>>\n\n"
        "Extract all AVAILABILITY and TACTICAL evidence. Return one JSON object."
    ),
)


#: Registry so a persisted ``prompt_template_id`` can be resolved back to the
#: template that produced it.
EXTRACTION_TEMPLATES: dict[str, PromptTemplate] = {
    AVAILABILITY_EXTRACTION.template_id: AVAILABILITY_EXTRACTION,
    TACTICAL_EXTRACTION.template_id: TACTICAL_EXTRACTION,
    COMBINED_EXTRACTION.template_id: COMBINED_EXTRACTION,
}


def get_template(template_id: str) -> PromptTemplate:
    """Resolve a template by id, raising a clear error for unknown ids."""
    try:
        return EXTRACTION_TEMPLATES[template_id]
    except KeyError:
        known = ", ".join(sorted(EXTRACTION_TEMPLATES))
        raise KeyError(f"Unknown extraction template '{template_id}'. Known: {known}") from None


def describe_templates() -> str:
    """Human-readable inventory, used by the architecture doc and audits."""
    return json.dumps(
        {
            tid: {"version": t.version, "schema_version": t.schema_version}
            for tid, t in sorted(EXTRACTION_TEMPLATES.items())
        },
        indent=2,
    )
