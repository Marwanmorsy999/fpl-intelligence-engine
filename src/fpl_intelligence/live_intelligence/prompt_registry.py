"""Phase 9.1 prompt versioning engine — cryptographic identity for every prompt.

A prompt is part of the method, not part of the plumbing. Two extractions that
carry the same ``prompt_hash`` were produced by *byte-identical* instructions;
two that differ were not, and any comparison between them is a comparison of
two different experiments. This module makes that distinction mechanical
instead of aspirational.

Two hashes, two questions
-------------------------

**Template hash** — SHA-256 over the template's canonical payload:
``template_id``, ``version``, ``schema_version``, the system block and the
*unrendered* user block. It answers *"which prompt design produced this?"* and
is stable across every input the template is ever rendered with.

**Rendered hash** — SHA-256 over the exact bytes sent to the model, i.e. the
template hash's inputs with the user block already substituted
(:meth:`~fpl_intelligence.live_intelligence.prompts.LLMPrompt.hash`). It answers
*"can I reproduce this individual call?"* and is what keys the response cache.

The lock file
-------------

:data:`PROMPT_HASH_LOCK` pins the template hash of every registered template.
:func:`verify_prompt_registry` recomputes them and reports any drift, and a unit
test asserts the report is empty. The effect is that a prompt cannot be edited
silently: changing one word fails the suite until the author *also* bumps
``version`` and updates the lock, which is exactly the moment to ask whether
previously-collected evidence is still comparable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from fpl_intelligence.live_intelligence.analyst_prompts import ANALYST_TEMPLATES
from fpl_intelligence.live_intelligence.prompts import (
    EXTRACTION_TEMPLATES,
    LLMPrompt,
    PromptTemplate,
)
from fpl_intelligence.live_intelligence.temporal_ledger import normalize_text

#: Named so a future migration to a different digest is explicit in the data.
PROMPT_HASH_ALGORITHM = "sha256"

#: Bumped when the *hashing scheme itself* changes (field set, ordering,
#: encoding). Distinct from a template's own ``version``: this invalidates
#: every stored hash at once, so it must never be changed casually.
PROMPT_HASH_SCHEME_VERSION = "phase9.prompthash.v1"


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_template_payload(template: PromptTemplate) -> dict[str, str]:
    """Exact, ordered set of fields that define a template's identity.

    Deliberately excludes anything derived or environmental (provider, model,
    temperature). Those belong to the *call*, not to the prompt, and mixing
    them in would make an unchanged prompt appear to change when the provider
    is swapped.
    """
    return {
        "scheme": PROMPT_HASH_SCHEME_VERSION,
        "template_id": template.template_id,
        "version": template.version,
        "schema_version": template.schema_version,
        "system": template.system,
        "user": template.user,
    }


def hash_prompt_template(template: PromptTemplate) -> str:
    """SHA-256 of the canonical template payload, including the schema version.

    Deterministic across processes and platforms: the payload is serialised
    with sorted keys, no ASCII escaping and a fixed separator set, so the digest
    depends on the template's content alone.
    """
    payload = json.dumps(
        canonical_template_payload(template),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _sha256(payload)


def hash_rendered_prompt(prompt: LLMPrompt) -> str:
    """SHA-256 of the exact bytes that would be sent to the model."""
    return prompt.hash()


def hash_input_text(text: str) -> str:
    """SHA-256 of whitespace-normalised source text.

    Normalising first means a re-paste of the same transcript with different
    line wrapping is recognised as the same input, which is what makes the
    response cache actually spare a free-tier quota.
    """
    return _sha256(normalize_text(text or ""))


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptFingerprint:
    """The versioning identity attached to an extraction run and its evidence."""

    template_id: str
    template_version: str
    schema_version: str
    template_hash: str
    rendered_hash: str | None = None
    algorithm: str = PROMPT_HASH_ALGORITHM
    scheme_version: str = PROMPT_HASH_SCHEME_VERSION

    @property
    def short(self) -> str:
        """Twelve hex characters — enough to eyeball in a log, never to forge."""
        return self.template_hash[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "template_version": self.template_version,
            "schema_version": self.schema_version,
            "template_hash": self.template_hash,
            "rendered_hash": self.rendered_hash,
            "algorithm": self.algorithm,
            "scheme_version": self.scheme_version,
        }


def fingerprint_template(template: PromptTemplate) -> PromptFingerprint:
    """Fingerprint a template with no rendered hash (no input bound yet)."""
    return PromptFingerprint(
        template_id=template.template_id,
        template_version=template.version,
        schema_version=template.schema_version,
        template_hash=hash_prompt_template(template),
    )


def fingerprint_prompt(
    prompt: LLMPrompt, *, template: PromptTemplate | None = None
) -> PromptFingerprint:
    """Fingerprint a rendered prompt, resolving its template when known.

    An unregistered template (an ad-hoc prompt built in a test or a notebook)
    still gets a rendered hash; only the template hash requires registration,
    and its absence is reported as an empty string rather than guessed at.
    """
    resolved = template or ALL_TEMPLATES.get(prompt.template_id)
    template_hash = hash_prompt_template(resolved) if resolved else ""
    return PromptFingerprint(
        template_id=prompt.template_id,
        template_version=prompt.version,
        schema_version=prompt.schema_version,
        template_hash=template_hash,
        rendered_hash=prompt.hash(),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Every template Phase 9 may send to a model, extraction and analyst alike.
ALL_TEMPLATES: dict[str, PromptTemplate] = {**EXTRACTION_TEMPLATES, **ANALYST_TEMPLATES}


@dataclass(frozen=True)
class LockedPrompt:
    """A pinned template identity. Changing any field is a deliberate act."""

    version: str
    schema_version: str
    template_hash: str


#: Pinned hashes. To change a prompt: edit it, bump its ``version``, run
#: ``python -m fpl_intelligence.live_intelligence.prompt_registry`` and paste
#: the emitted block here. Never update the hash without bumping the version —
#: that is precisely the silent change this lock exists to prevent.
PROMPT_HASH_LOCK: dict[str, LockedPrompt] = {
    "assistant.brief": LockedPrompt(
        version="1.0.0",
        schema_version="1.0.0",
        template_hash="23b2be5426a96d6f89f39245f4ca8f5956fc1720a64884059bd1d41864820497",
    ),
    "phase18.analyst.summary": LockedPrompt(
        version="1.0.0",
        schema_version="1.0.0",
        template_hash="df9336296fdb85fbed071297d7bd2bd9229bc0d66628e63b14825611d5ef59ec",
    ),
    "phase9.analyst.captaincy": LockedPrompt(
        version="1.0.0",
        schema_version="phase9.analyst.v1",
        template_hash="2e3ae089c7d6577e491b5d9008a8ef8b6ac0c6d35a8b2bef084430e83c7e67b9",
    ),
    "phase9.analyst.differential": LockedPrompt(
        version="1.0.0",
        schema_version="phase9.analyst.v1",
        template_hash="6a414aac2a4bb6673c9464d2d7766b6e698e2ff3435f55eaec722417b91f81bd",
    ),
    "phase9.analyst.transfer": LockedPrompt(
        version="1.0.0",
        schema_version="phase9.analyst.v1",
        template_hash="5eb1fcd6ff9d9d4aa90f6b7b2b78dff59b4827c66815f11f5f68199dbfbdf66b",
    ),
    "phase9.extract.availability": LockedPrompt(
        version="1.1.0",
        schema_version="phase9.extraction.v1",
        template_hash="f7c8a3de7caedad868a51e3db09f127622e6a5c809762a6b1c24642a1aaf1400",
    ),
    "phase9.extract.combined": LockedPrompt(
        version="1.1.0",
        schema_version="phase9.extraction.v1",
        template_hash="af0cb123bb3c3a1eec6a524268bfaba9cca1201b6aee98894a52485eb23e331d",
    ),
    "phase9.extract.tactical": LockedPrompt(
        version="1.1.0",
        schema_version="phase9.extraction.v1",
        template_hash="c32c2f5ae8fb0783dc073e4d2cd16953e523946ce3429f8f55f00d8b5f1138a4",
    ),
}


@dataclass(frozen=True)
class PromptDrift:
    """One discrepancy between a live template and the lock."""

    template_id: str
    field: str
    expected: str
    actual: str

    def __str__(self) -> str:
        return (
            f"{self.template_id}: {self.field} drifted — "
            f"locked {self.expected!r}, live {self.actual!r}"
        )


class PromptDriftError(RuntimeError):
    """Raised when a registered prompt no longer matches its pinned hash."""


def verify_prompt_registry(
    templates: dict[str, PromptTemplate] | None = None,
    lock: dict[str, LockedPrompt] | None = None,
) -> list[PromptDrift]:
    """Compare live templates against the lock and return every discrepancy.

    Reports, rather than raises, so a caller can show *all* drift at once
    instead of one item per run.
    """
    live = templates if templates is not None else ALL_TEMPLATES
    pinned = lock if lock is not None else PROMPT_HASH_LOCK
    drift: list[PromptDrift] = []

    for template_id in sorted(set(live) | set(pinned)):
        template = live.get(template_id)
        locked = pinned.get(template_id)
        if template is None:
            drift.append(
                PromptDrift(template_id, "existence", "registered", "missing from registry")
            )
            continue
        if locked is None:
            drift.append(
                PromptDrift(template_id, "existence", "absent from lock", "registered template")
            )
            continue
        if template.version != locked.version:
            drift.append(PromptDrift(template_id, "version", locked.version, template.version))
        if template.schema_version != locked.schema_version:
            drift.append(
                PromptDrift(
                    template_id, "schema_version", locked.schema_version, template.schema_version
                )
            )
        actual_hash = hash_prompt_template(template)
        if actual_hash != locked.template_hash:
            drift.append(
                PromptDrift(template_id, "template_hash", locked.template_hash, actual_hash)
            )
    return drift


def assert_prompt_registry_locked() -> None:
    """Raise :class:`PromptDriftError` if any registered prompt has drifted."""
    drift = verify_prompt_registry()
    if not drift:
        return
    details = "\n  ".join(str(d) for d in drift)
    raise PromptDriftError(
        "Prompt templates no longer match PROMPT_HASH_LOCK:\n  "
        f"{details}\n\n"
        "A prompt change alters the method, so previously extracted evidence is "
        "not directly comparable to anything extracted after it. If the change "
        "is intended: bump the template's `version`, then run\n"
        "  python -m fpl_intelligence.live_intelligence.prompt_registry\n"
        "and paste the emitted lock block into PROMPT_HASH_LOCK."
    )


def registry_report() -> dict[str, Any]:
    """Machine-readable inventory of every registered prompt and its hashes."""
    return {
        "algorithm": PROMPT_HASH_ALGORITHM,
        "scheme_version": PROMPT_HASH_SCHEME_VERSION,
        "templates": {
            template_id: fingerprint_template(template).to_dict()
            for template_id, template in sorted(ALL_TEMPLATES.items())
        },
        "drift": [str(d) for d in verify_prompt_registry()],
    }


def render_lock_block() -> str:
    """Emit a paste-ready ``PROMPT_HASH_LOCK`` body for the current templates."""
    lines = ["PROMPT_HASH_LOCK: dict[str, LockedPrompt] = {"]
    for template_id, template in sorted(ALL_TEMPLATES.items()):
        lines.extend(
            [
                f'    "{template_id}": LockedPrompt(',
                f'        version="{template.version}",',
                f'        schema_version="{template.schema_version}",',
                f'        template_hash="{hash_prompt_template(template)}",',
                "    ),",
            ]
        )
    lines.append("}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - developer utility
    print(render_lock_block())
