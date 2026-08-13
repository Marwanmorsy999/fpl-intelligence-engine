"""Deterministic mock LLM provider — no network, no randomness, no API keys.

This is a **test double**, not a simulation of a language model. It exists so
that the whole Phase 9 pipeline (prompting, parsing, schema validation,
grounding, temporal inheritance, persistence, analyst synthesis) can be
exercised end-to-end and asserted on exactly, before a single real token is
spent or a single live page is scraped.

Determinism guarantees
----------------------

* The same prompt always yields byte-identical output.
* No clock, no RNG, no I/O, no environment lookups.
* Rule-derived quotes are sliced out of the input text, so they are grounded by
  construction and the grounding check passes for legitimate output.

Honesty guarantees
------------------

* :attr:`MockLLMProvider.is_mock` is ``True``, which propagates to
  ``llm_extraction_runs.is_mock`` and therefore excludes everything it produces
  from validation-evidence queries. Mock output can never be counted as real.

Escape hatches for tests
------------------------

* ``scripted``: map a prompt hash (or ``"*"``) to an exact response string, for
  asserting on malformed JSON, schema violations or hallucinated quotes.
* ``rules``: replace the default keyword rules entirely.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from fpl_intelligence.availability.models import AvailabilityStatus, EvidenceType
from fpl_intelligence.live_intelligence.extraction import LLMProvider, LLMResponse
from fpl_intelligence.live_intelligence.models import (
    TacticalDirection,
    TacticalEvidenceType,
)
from fpl_intelligence.live_intelligence.prompts import LLMPrompt
from fpl_intelligence.live_intelligence.schemas import (
    ANALYST_SCHEMA_VERSION,
    EXTRACTION_SCHEMA_VERSION,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# Compiled word-boundary patterns, memoised per keyword. Word boundaries matter:
# the rule keyword "available" must *not* fire inside the word "unavailable", and
# "out" must not fire inside "about". A plain ``substring in text`` check would
# let a negative statement ("he is unavailable") be misread as a positive one.
_KEYWORD_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    cached = _KEYWORD_PATTERN_CACHE.get(keyword)
    if cached is None:
        cached = re.compile(r"\b" + re.escape(keyword) + r"\b")
        _KEYWORD_PATTERN_CACHE[keyword] = cached
    return cached


def _keyword_match(keyword: str, text: str) -> bool:
    """True when *keyword* appears as a whole word/phrase in *text*."""
    return bool(_keyword_pattern(keyword).search(text))



@dataclass(frozen=True)
class KeywordRule:
    """Maps a keyword to a deterministic piece of extracted evidence.

    ``kind`` is ``"availability"`` or ``"tactical"``. Only the fields relevant
    to that kind are used.
    """

    keyword: str
    kind: str
    evidence_type: str
    status: str | None = None
    direction: str = TacticalDirection.UNKNOWN
    confidence: float = 0.6
    value_from_sentence: bool = False


#: Default rules. Chosen to cover the Phase 7 availability taxonomy and the
#: Phase 8 tactical taxonomy with unambiguous English trigger words.
DEFAULT_RULES: tuple[KeywordRule, ...] = (
    # -- availability --
    KeywordRule(
        "ruled out", "availability", EvidenceType.INJURY,
        AvailabilityStatus.OUT, confidence=0.9,
    ),
    KeywordRule(
        "out for", "availability", EvidenceType.INJURY,
        AvailabilityStatus.OUT, confidence=0.85,
    ),
    KeywordRule(
        "injured", "availability", EvidenceType.INJURY,
        AvailabilityStatus.OUT, confidence=0.8,
    ),
    KeywordRule(
        "suspended", "availability", EvidenceType.SUSPENSION,
        AvailabilityStatus.SUSPENDED, confidence=0.9,
    ),
    KeywordRule(
        "doubt", "availability", EvidenceType.FITNESS,
        AvailabilityStatus.DOUBTFUL, confidence=0.7,
    ),
    KeywordRule(
        "touch and go", "availability", EvidenceType.FITNESS,
        AvailabilityStatus.QUESTIONABLE, confidence=0.6,
    ),
    KeywordRule(
        "back in training", "availability", EvidenceType.TRAINING,
        AvailabilityStatus.SUSPECT, confidence=0.75,
    ),
    KeywordRule(
        "trained fully", "availability", EvidenceType.TRAINING,
        AvailabilityStatus.START, confidence=0.8,
    ),
    KeywordRule(
        "will start", "availability", EvidenceType.LINEUP_HINT,
        AvailabilityStatus.START, confidence=0.85,
    ),
    KeywordRule(
        "on the bench", "availability", EvidenceType.LINEUP_HINT,
        AvailabilityStatus.BENCH, confidence=0.7,
    ),
    KeywordRule(
        "unavailable", "availability", EvidenceType.FITNESS,
        AvailabilityStatus.DOUBTFUL, confidence=0.75,
    ),
    KeywordRule(
        "unavailable", "availability", EvidenceType.FITNESS,
        AvailabilityStatus.DOUBTFUL, confidence=0.75,
    ),
    KeywordRule(
        "available", "availability", EvidenceType.FITNESS,
        AvailabilityStatus.AVAILABLE, confidence=0.7,
    ),
    # -- tactical --
    KeywordRule(
        "formation", "tactical", TacticalEvidenceType.FORMATION,
        value_from_sentence=True, confidence=0.7,
    ),
    KeywordRule(
        "penalties", "tactical", TacticalEvidenceType.SET_PIECE_PENALTIES,
        direction=TacticalDirection.POSITIVE, confidence=0.8,
    ),
    KeywordRule(
        "free kicks", "tactical", TacticalEvidenceType.SET_PIECE_FREEKICKS,
        direction=TacticalDirection.POSITIVE, confidence=0.75,
    ),
    KeywordRule(
        "corners", "tactical", TacticalEvidenceType.SET_PIECE_CORNERS,
        direction=TacticalDirection.POSITIVE, confidence=0.75,
    ),
    KeywordRule(
        "rotate", "tactical", TacticalEvidenceType.ROTATION_TENDENCY,
        direction=TacticalDirection.NEGATIVE, confidence=0.65,
    ),
    KeywordRule(
        "rested", "tactical", TacticalEvidenceType.ROTATION_TENDENCY,
        direction=TacticalDirection.NEGATIVE, confidence=0.7,
    ),
    KeywordRule(
        "new role", "tactical", TacticalEvidenceType.ROLE_CHANGE,
        direction=TacticalDirection.UNKNOWN, confidence=0.6,
    ),
    KeywordRule(
        "play him further forward", "tactical",
        TacticalEvidenceType.ROLE_CHANGE,
        direction=TacticalDirection.POSITIVE, confidence=0.7,
    ),
    KeywordRule(
        "new manager", "tactical", TacticalEvidenceType.MANAGER_CHANGE,
        confidence=0.85,
    ),
)

_FORMATION_RE = re.compile(r"\b\d(?:-\d){1,3}\b")


class MockLLMProvider(LLMProvider):
    """Deterministic, offline stand-in for a real LLM provider.

    Args:
        scripted: Exact responses keyed by ``prompt.hash()``. The key ``"*"``
            matches any prompt and takes lowest precedence. Use this to force
            malformed or adversarial output in tests.
        rules: Keyword rules for extraction prompts. Defaults to
            :data:`DEFAULT_RULES`.
        player_names: Names the mock is willing to attribute evidence to. A
            rule only fires when one of these appears in the same sentence,
            which keeps output attributable instead of anonymous.
        model_name: Reported model identifier.
        latency_ms: Fixed reported latency, so provenance stays deterministic.
    """

    def __init__(
        self,
        *,
        scripted: dict[str, str] | None = None,
        rules: Sequence[KeywordRule] | None = None,
        player_names: Sequence[str] = (),
        model_name: str = "mock-deterministic-v1",
        latency_ms: int = 0,
    ) -> None:
        self._scripted = dict(scripted or {})
        self._rules = tuple(rules) if rules is not None else DEFAULT_RULES
        self._player_names = tuple(player_names)
        self._model_name = model_name
        self._latency_ms = latency_ms
        self.calls: list[LLMPrompt] = []

    # -- LLMProvider ------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def is_mock(self) -> bool:
        return True

    def complete(self, prompt: LLMPrompt) -> LLMResponse:
        self.calls.append(prompt)
        text = self._scripted.get(prompt.hash()) or self._scripted.get("*")
        if text is None:
            text = self._generate(prompt)
        return LLMResponse(
            text=text,
            provider_name=self.provider_name,
            model_name=self._model_name,
            is_mock=True,
            latency_ms=self._latency_ms,
            temperature=0.0,
        )

    # -- generation --------------------------------------------------------

    def _generate(self, prompt: LLMPrompt) -> str:
        if prompt.template_id.startswith("phase9.analyst"):
            return self._generate_analyst(prompt)
        return self._generate_extraction(prompt)

    def _generate_extraction(self, prompt: LLMPrompt) -> str:
        raw_text: str = prompt.context.get("raw_text", "")
        want_availability = "availability" in prompt.template_id or "combined" in prompt.template_id
        want_tactical = "tactical" in prompt.template_id or "combined" in prompt.template_id

        availability: list[dict[str, Any]] = []
        tactical: list[dict[str, Any]] = []
        team_hint = prompt.context.get("team_hint")

        for sentence in _sentences(raw_text):
            lowered = sentence.casefold()
            subject = self._subject_in(sentence)
            for rule in self._rules:
                if not _keyword_match(rule.keyword, lowered):
                    continue
                if rule.kind == "availability" and want_availability:
                    if subject is None:
                        continue
                    availability.append(
                        {
                            "player_name": subject,
                            "team_name": team_hint,
                            "evidence_type": rule.evidence_type,
                            "status_mentioned": rule.status,
                            "confidence": rule.confidence,
                            "expected_absence_gameweeks": None,
                            "source_quote": sentence,
                            "reasoning": f"Sentence states '{rule.keyword}'.",
                        }
                    )
                elif rule.kind == "tactical" and want_tactical:
                    value = None
                    if rule.value_from_sentence:
                        match = _FORMATION_RE.search(sentence)
                        value = match.group(0) if match else None
                    tactical.append(
                        {
                            "evidence_type": rule.evidence_type,
                            "team_name": team_hint,
                            "player_name": subject,
                            "value_text": value,
                            "numeric_value": None,
                            "direction": rule.direction,
                            "confidence": rule.confidence,
                            "source_quote": sentence,
                            "reasoning": f"Sentence states '{rule.keyword}'.",
                        }
                    )

        envelope = {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "availability_evidence": availability,
            "tactical_evidence": tactical,
            "no_evidence_found": not availability and not tactical,
            "extraction_notes": "" if (availability or tactical) else "no trigger phrases matched",
        }
        return json.dumps(envelope, indent=2, sort_keys=True)

    def _generate_analyst(self, prompt: LLMPrompt) -> str:
        """Echo the supplied baseline and cite the supplied evidence.

        The mock never invents a number: it restates exactly what the
        quantitative layer gave it, which is precisely the behaviour the
        analyst guardrails demand of a real model.
        """
        context = prompt.context
        baselines: list[dict[str, Any]] = list(context.get("baselines", []))
        evidence: list[dict[str, Any]] = list(context.get("evidence", []))
        task: str = context.get("task", "transfer_recommendation")

        citations = [
            {
                "subject_ref": b["subject_ref"],
                "expected_points": b["expected_points"],
                "start_probability": b["start_probability"],
                "floor": b["floor"],
                "ceiling": b["ceiling"],
                "interpretation": (
                    f"Quantitative engine projects {b['expected_points']} pts for "
                    f"{b['subject_ref']} with start probability {b['start_probability']}."
                ),
            }
            for b in baselines
        ]

        refs = [e["evidence_ref"] for e in evidence]
        direction, magnitude = _mock_direction(evidence)
        recommendation = _mock_recommendation(direction, bool(evidence))

        output = {
            "schema_version": ANALYST_SCHEMA_VERSION,
            "task": task,
            "headline": _mock_headline(task, context.get("subject_label", "subject")),
            "quantitative_baseline": citations,
            "qualitative_adjustment": {
                "direction": direction,
                "magnitude": magnitude,
                "cited_evidence_refs": refs,
                "rationale": (
                    "Qualitative evidence "
                    + ("supports a " + direction + " adjustment." if refs else "is absent.")
                ),
            },
            "net_assessment": (
                "The quantitative baseline is stated above and is unchanged. The "
                "qualitative layer only shifts conviction, never the projection."
            ),
            "recommendation": recommendation,
            "confidence": 0.6 if refs else 0.4,
            "caveats": [
                "Phase 9 scaffold output produced by a deterministic mock provider; "
                "not real model reasoning and not validation evidence.",
            ],
        }
        return json.dumps(output, indent=2, sort_keys=True)

    # -- helpers -----------------------------------------------------------

    def _subject_in(self, sentence: str) -> str | None:
        """Return the first configured player name occurring in the sentence."""
        lowered = sentence.casefold()
        for name in self._player_names:
            if name.casefold() in lowered:
                return name
        return None


def _sentences(text: str) -> list[str]:
    """Split into sentences, preserving each verbatim so quotes stay grounded."""
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]


def _mock_direction(evidence: Sequence[dict[str, Any]]) -> tuple[str, str]:
    """Derive a deterministic direction/magnitude from the evidence bundle."""
    if not evidence:
        return "neutral", "none"
    negatives = sum(1 for e in evidence if e.get("direction") == "negative")
    positives = sum(1 for e in evidence if e.get("direction") == "positive")
    if negatives > positives:
        direction = "down"
    elif positives > negatives:
        direction = "up"
    else:
        direction = "neutral"
    magnitude = "moderate" if len(evidence) > 1 else "low"
    if direction == "neutral":
        magnitude = "none" if not evidence else "low"
    return direction, magnitude


def _mock_recommendation(direction: str, has_evidence: bool) -> str:
    if not has_evidence:
        return "no_recommendation"
    if direction == "down":
        return "avoid"
    if direction == "up":
        return "proceed"
    return "monitor"


def _mock_headline(task: str, subject_label: str) -> str:
    titles: dict[str, str] = {
        "transfer_recommendation": f"Transfer view: {subject_label}",
        "captaincy_debate": f"Captaincy debate: {subject_label}",
        "differential_risk": f"Differential risk profile: {subject_label}",
    }
    return titles.get(task, f"Analysis: {subject_label}")


#: Convenience factory used by tests and the architecture doc's worked example.
def make_mock_provider(
    player_names: Sequence[str] = (),
    *,
    scripted: dict[str, str] | None = None,
    rules: Sequence[KeywordRule] | None = None,
) -> MockLLMProvider:
    """Build a :class:`MockLLMProvider` with the given attributable names."""
    return MockLLMProvider(player_names=player_names, scripted=scripted, rules=rules)


#: Typing alias kept for callers that want to plug in their own generator.
MockGenerator = Callable[[LLMPrompt], str]
