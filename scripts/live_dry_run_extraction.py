#!/usr/bin/env python
"""Phase 9.1 live dry-run — exercise the extraction pipeline against a real LLM.

This is a **manual tool, not a test**. It lives in ``scripts/`` and is never
collected by pytest, because the automated suite must stay free, offline and
deterministic. Running it spends real free-tier quota, so every safeguard the
engine has is switched on and reported.

What it does
------------

1. Loads credentials from the local, git-ignored ``.env`` (never from source).
2. Prints a **redacted** configuration banner — keys appear only as SHA-256
   fingerprints.
3. Asks for confirmation before spending quota (skip with ``--yes``).
4. Builds a real provider through :class:`ProviderFactory`, wired to the
   response cache, the token cap, the rate limiter and the call budget.
5. Feeds a realistic pre-match press-conference transcript through the very
   same :class:`PromptedLLMExtractor` the engine uses.
6. Prints the raw response, the parsed JSON envelope, and then **verifies**
   that every extracted item maps onto the Phase 7 ``availability_evidence``
   and Phase 9/8 ``tactical_evidence`` schemas.

Usage
-----

::

    python scripts/live_dry_run_extraction.py                     # offline mock
    python scripts/live_dry_run_extraction.py --provider groq
    python scripts/live_dry_run_extraction.py --provider gemini --yes
    python scripts/live_dry_run_extraction.py --provider openrouter \\
        --model "meta-llama/llama-3.3-70b-instruct:free"
    python scripts/live_dry_run_extraction.py --provider groq --text-file my.txt
    python scripts/live_dry_run_extraction.py --router \\
        --template phase9.extract.availability

Exit codes: ``0`` success, ``1`` configuration error, ``2`` provider error,
``3`` schema-verification failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(_SRC))

from fpl_intelligence.availability.models import (  # noqa: E402
    AvailabilityEvidence,
    AvailabilityStatus,
    EvidenceType,
)
from fpl_intelligence.live_intelligence.extraction import (  # noqa: E402
    ExtractionResult,
    ExtractionStatus,
    LLMProvider,
    LLMProviderError,
    PromptedLLMExtractor,
)
from fpl_intelligence.live_intelligence.llm_providers import ProviderFactory  # noqa: E402
from fpl_intelligence.live_intelligence.llm_settings import (  # noqa: E402
    LLMProviderName,
    LLMSettingsError,
    load_llm_settings,
)
from fpl_intelligence.live_intelligence.models import (  # noqa: E402
    LedgerTemporalClass,
    TacticalDirection,
    TacticalEvidence,
    TacticalEvidenceType,
)
from fpl_intelligence.live_intelligence.prompt_registry import (  # noqa: E402
    fingerprint_prompt,
    verify_prompt_registry,
)
from fpl_intelligence.live_intelligence.prompts import get_template  # noqa: E402
from fpl_intelligence.live_intelligence.provider_router import (  # noqa: E402
    ProviderRouter,
)
from fpl_intelligence.live_intelligence.response_cache import ResponseCache  # noqa: E402
from fpl_intelligence.live_intelligence.temporal_ledger import (  # noqa: E402
    LedgerItemView,
    build_timestamps,
    classify_ledger_entry,
)

DEFAULT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "press_conference_transcript.txt"

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_PROVIDER = 2
EXIT_VERIFICATION = 3


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------


def rule(title: str = "") -> None:
    if title:
        print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    else:
        print("-" * 78)


def kv(label: str, value: Any, width: int = 28) -> None:
    print(f"  {label.ljust(width)} {value}")


# ---------------------------------------------------------------------------
# Ledger construction (no database required)
# ---------------------------------------------------------------------------


def build_ledger_view(raw_text: str, *, source_name: str, team_hint: str) -> LedgerItemView:
    """Wrap the fixture text in a fully-validated ledger view.

    The dry run deliberately goes through :func:`build_timestamps` and
    :func:`classify_ledger_entry` rather than hand-building the view: the point
    is to exercise the real temporal contract, including the derivation of
    ``available_at`` and the pre/post-deadline classification, not just the
    provider call.
    """
    now = datetime.now(UTC)
    published_at = now - timedelta(minutes=30)
    timestamps = build_timestamps(
        scraped_at=now - timedelta(minutes=5),
        ingested_at=now,
        published_at=published_at,
        now=now,
    )
    # A deadline in the future makes this PRE_DEADLINE, which is the only class
    # that could ever inform a decision.
    deadline = now + timedelta(days=1)
    temporal_class = classify_ledger_entry(timestamps, deadline)

    return LedgerItemView(
        raw_item_id=None,
        raw_text=raw_text,
        title="Pre-match press conference (dry run)",
        source_name=source_name,
        source_type="press_conference",
        source_reliability="unverified",
        environment="mock",  # a dry run is never validation evidence
        timestamps=timestamps,
        temporal_class=str(temporal_class),
        team_hint=team_hint,
        deadline_at=deadline,
    )


# ---------------------------------------------------------------------------
# Schema verification
# ---------------------------------------------------------------------------


def verify_schema_mapping(result: ExtractionResult) -> list[str]:
    """Check every accepted draft against the Phase 7 / Phase 8 schemas.

    Two levels of check, because passing Pydantic validation is necessary but
    not sufficient:

    * **Taxonomy** — the categorical values must be members of the *existing*
      Phase 7 (``EvidenceType`` / ``AvailabilityStatus``) and Phase 8
      (``TacticalEvidenceType`` / ``TacticalDirection``) enums. Phase 9 is not
      allowed to invent a new category.
    * **Persistence shape** — the draft must actually construct the ORM row it
      claims to map onto. This catches a field that validates in isolation but
      cannot be written.

    Returns a list of human-readable problems; empty means everything mapped.
    """
    problems: list[str] = []
    now = datetime.now(UTC)

    for index, draft in enumerate(result.availability):
        label = f"availability[{index}] ({draft.player_name})"
        if draft.evidence_type not in set(EvidenceType):
            problems.append(
                f"{label}: evidence_type {draft.evidence_type!r} is not a "
                "Phase 7 EvidenceType"
            )
        if draft.status_mentioned not in set(AvailabilityStatus):
            problems.append(
                f"{label}: status_mentioned {draft.status_mentioned!r} is not "
                "a Phase 7 AvailabilityStatus"
            )
        if not 0.0 <= draft.confidence <= 1.0:
            problems.append(f"{label}: confidence {draft.confidence} outside [0, 1]")
        if not draft.prompt_hash:
            problems.append(f"{label}: no prompt_hash attached — provenance is incomplete")
        if not draft.provider_name:
            problems.append(f"{label}: no provider_name attached — provenance is incomplete")
        try:
            AvailabilityEvidence(
                player_id=0,
                season_id=0,
                evidence_type=draft.evidence_type,
                status_mentioned=draft.status_mentioned,
                confidence=draft.confidence,
                description=draft.source_quote,
                extracted_at=now,
                valid_from=draft.available_at,
                is_active=True,
            )
        except Exception as exc:  # noqa: BLE001 - report, do not crash the run
            problems.append(f"{label}: does not map onto availability_evidence: {exc}")

    for index, draft in enumerate(result.tactical):
        label = f"tactical[{index}] ({draft.subject_hint})"
        if draft.evidence_type not in set(TacticalEvidenceType):
            problems.append(
                f"{label}: evidence_type {draft.evidence_type!r} is not a "
                "Phase 8 TacticalEvidenceType"
            )
        if draft.direction not in set(TacticalDirection):
            problems.append(f"{label}: direction {draft.direction!r} is not a TacticalDirection")
        if not 0.0 <= draft.confidence <= 1.0:
            problems.append(f"{label}: confidence {draft.confidence} outside [0, 1]")
        if not draft.prompt_hash:
            problems.append(f"{label}: no prompt_hash attached — provenance is incomplete")
        try:
            TacticalEvidence(
                raw_item_id=0,
                subject_hint=draft.subject_hint,
                evidence_type=draft.evidence_type,
                value_text=draft.value_text,
                numeric_value=draft.numeric_value,
                direction=draft.direction,
                confidence=draft.confidence,
                source_quote=draft.source_quote,
                published_at=draft.published_at,
                available_at=draft.available_at,
                ingested_at=draft.ingested_at,
                extracted_at=now,
                temporal_class=draft.temporal_class,
                prompt_hash=draft.prompt_hash,
                provider_name=draft.provider_name,
                model_name=draft.model_name,
                valid_from=draft.available_at,
                is_active=True,
            )
        except Exception as exc:  # noqa: BLE001 - report, do not crash the run
            problems.append(f"{label}: does not map onto tactical_evidence: {exc}")

    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 9.1 live dry-run extraction against a real LLM provider.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--provider",
        choices=[p.value for p in LLMProviderName],
        default=None,
        help="Provider to use. Defaults to LLM_PROVIDER in .env, else 'mock' (offline).",
    )
    parser.add_argument("--model", default=None, help="Override the provider's default model.")
    parser.add_argument(
        "--text-file",
        type=Path,
        default=DEFAULT_FIXTURE,
        help=f"Unstructured text to extract from (default: {DEFAULT_FIXTURE.name}).",
    )
    parser.add_argument(
        "--template",
        default="phase9.extract.combined",
        help="Extraction template id (combined / availability / tactical).",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="Hard generation cap for this run. Defaults to LLM_MAX_OUTPUT_TOKENS.",
    )
    parser.add_argument(
        "--router",
        action="store_true",
        help="Route the task through the ProviderRouter instead of a single fixed "
        "provider: task-based routing with automatic fallback on rate-limit/auth "
        "errors. Uses only providers with a configured API key.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the response cache. Forces a real API call even on a repeat run.",
    )
    parser.add_argument(
        "--show-prompt", action="store_true", help="Print the fully rendered prompt."
    )
    parser.add_argument(
        "--json-out", type=Path, default=None, help="Write the full run report to this JSON file."
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip the quota-spend confirmation prompt."
    )
    return parser.parse_args(argv)


def confirm_spend(provider: LLMProviderName, model: str, *, assume_yes: bool) -> bool:
    """Gate a real API call behind an explicit acknowledgement.

    A dry run is cheap but not free. Making the spend explicit is what stops a
    stray shell-history re-run from quietly eating the day's allowance.
    """
    if not provider.is_real or assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "\nRefusing to make live API calls without confirmation in a non-interactive "
            "session. Re-run with --yes if that is what you intend."
        )
        return False
    answer = input(f"\nThis will call the real {provider} API ({model}). Continue? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def confirm_router_spend(*, assume_yes: bool) -> bool:
    """Gate a router run (potentially several providers) behind acknowledgement.

    The router chooses the provider at call time and may fall back to another,
    so there is no single ``(provider, model)`` to name up front — confirm the
    intent to spend quota across one or more configured providers instead.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "\nRefusing to make live API calls without confirmation in a non-interactive "
            "session. Re-run with --yes if that is what you intend."
        )
        return False
    answer = input(
        "\nThis will call one or more real LLM APIs selected by the ProviderRouter "
        "(task-based routing with fallback). Continue? [y/N] "
    )
    return answer.strip().lower() in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    rule("PHASE 9.1 — LIVE DRY-RUN EXTRACTION")
    print("Manual tool. Not collected by pytest. Spends real free-tier quota when")
    print("a real provider is selected.\n")

    # -- 1. configuration ---------------------------------------------------
    using_router = args.router
    requested = LLMProviderName(args.provider) if args.provider else None
    needs_env = using_router or requested is None or requested.is_real
    overrides: dict[str, Any] = {}
    if requested is not None:
        overrides["llm_provider"] = requested
    if args.model:
        overrides["llm_model"] = args.model
    if args.max_output_tokens is not None:
        overrides["llm_max_output_tokens"] = args.max_output_tokens
    if args.no_cache:
        overrides["llm_cache_enabled"] = False

    try:
        settings = load_llm_settings(require_env_file=needs_env, **overrides)
    except LLMSettingsError as exc:
        rule("CONFIGURATION ERROR")
        print(exc)
        return EXIT_CONFIG

    provider_name = settings.llm_provider
    model = settings.model_for()

    rule("CONFIGURATION (redacted)")
    for key, value in settings.describe().items():
        if key == "api_keys":
            print("  api_keys")
            for var, fingerprint in value.items():
                kv(f"  {var}", fingerprint, width=26)
        else:
            kv(key, value)

    if provider_name.is_real and not settings.has_api_key(provider_name):
        rule("CONFIGURATION ERROR")
        try:
            settings.require_api_key(provider_name)
        except LLMSettingsError as exc:
            print(exc)
        return EXIT_CONFIG

    # -- 2. prompt versioning ----------------------------------------------
    rule("PROMPT VERSIONING")
    template = get_template(args.template)
    drift = verify_prompt_registry()
    kv("template_id", template.template_id)
    kv("template_version", template.version)
    kv("schema_version", template.schema_version)
    kv("registry_drift", "none" if not drift else f"{len(drift)} item(s)")
    for item in drift:
        print(f"    ! {item}")

    # -- 3. source text -----------------------------------------------------
    text_path: Path = args.text_file
    if not text_path.is_file():
        rule("CONFIGURATION ERROR")
        print(f"Source text file not found: {text_path}")
        return EXIT_CONFIG
    raw_text = text_path.read_text(encoding="utf-8")

    rule("SOURCE TEXT")
    kv("file", text_path)
    kv("characters", len(raw_text))
    kv("input_char_limit", settings.llm_max_input_chars)
    print("\n" + "\n".join(f"  | {line}" for line in raw_text.splitlines()[:8]))
    print("  | ...")

    item = build_ledger_view(
        raw_text, source_name="dry_run_press_conference", team_hint="Fictional Rovers"
    )
    kv("temporal_class", item.temporal_class)
    kv("available_at", item.timestamps.available_at.isoformat())
    if item.temporal_class != LedgerTemporalClass.PRE_DEADLINE:
        print("  ! Not pre-deadline; this material could not inform a decision.")

    # -- 4. confirm and build the provider ---------------------------------
    factory = ProviderFactory(settings)
    mock_players = (
        "Marcus Ellery",
        "Danny Okoro",
        "Tomas Beier",
        "Kwame Asare",
        "Liam Verhoeven",
        "Rafael Duarte",
        "Sofiane Belkacem",
        "Owen Fitzgerald",
        "Jonah Pike",
    )

    provider: LLMProvider
    if using_router:
        if not confirm_router_spend(assume_yes=args.yes):
            print("\nAborted before any API call was made.")
            return EXIT_OK
        provider = ProviderRouter(factory, mock_player_names=mock_players)
        kv("routing", "enabled (task-based with fallback)")
        kv("preferred availability", "groq")
        kv("preferred tactical/combined", "gemini")
    else:
        if not confirm_spend(provider_name, model, assume_yes=args.yes):
            print("\nAborted before any API call was made.")
            return EXIT_OK
        try:
            provider = factory.create(provider_name, mock_player_names=mock_players)
        except LLMSettingsError as exc:
            rule("CONFIGURATION ERROR")
            print(exc)
            return EXIT_CONFIG

    extractor = PromptedLLMExtractor(provider, template=template)
    prompt = extractor.build_prompt(item)
    fingerprint = fingerprint_prompt(prompt, template=template)

    rule("PROMPT FINGERPRINT")
    for key, value in fingerprint.to_dict().items():
        kv(key, value)
    if args.show_prompt:
        rule("RENDERED PROMPT — SYSTEM")
        print(prompt.system)
        rule("RENDERED PROMPT — USER")
        print(prompt.user)

    # -- 5. extract ---------------------------------------------------------
    rule("CALLING PROVIDER")
    kv("provider", provider.provider_name)
    kv("model", provider.model_name)
    kv("is_mock", provider.is_mock)
    try:
        result = extractor.extract(item)
    except LLMProviderError as exc:
        rule("PROVIDER ERROR")
        print(exc)
        return EXIT_PROVIDER
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    if using_router:
        rule("ROUTING DECISION")
        last_route = getattr(provider, "last_route", None) or getattr(
            provider, "last_route_attempt", None
        )
        if last_route is not None:
            kv("provider", last_route.provider_name)
            kv("model", last_route.model_name)
            kv("routing_strategy", last_route.routing_strategy.value)
            kv("task", last_route.task)
            # Phase 9.1.1 — when fallback fired, surface why: the primary
            # provider attempted and the coarse failure reason. No secrets are
            # involved: provider names and a reason category only.
            if last_route.routing_strategy.value == "fallback":
                failure = getattr(provider, "last_failure", None)
                if failure is not None:
                    kv("primary provider attempted", failure.primary_provider)
                    kv("failure reason", failure.reason)
                else:
                    kv("primary provider attempted", "(unknown)")
                    kv("failure reason", "(unknown)")
        else:
            kv("routing", "no route recorded (call failed before routing?)")

    rule("RAW LLM RESPONSE")
    print(result.raw_response if result.raw_response else "<no response body>")

    rule("PARSED JSON ENVELOPE")
    if result.envelope is not None:
        print(json.dumps(result.envelope.to_dict(), indent=2))
    else:
        print("<envelope not produced>")
        kv("status", result.status)
        kv("error", result.error)

    # -- 6. verify against Phase 7 / Phase 8 schemas -----------------------
    rule("SCHEMA VERIFICATION")
    kv("extraction status", result.status)
    kv("availability drafts", len(result.availability))
    kv("tactical drafts", len(result.tactical))
    kv("rejected items", len(result.rejected))
    for rejected in result.rejected:
        print(f"    ! rejected {rejected.kind}: {rejected.reason}")

    problems = verify_schema_mapping(result)
    if problems:
        print("\n  FAILED — the following items do not map onto the target schemas:")
        for problem in problems:
            print(f"    ! {problem}")
    else:
        print("\n  OK — every accepted item maps onto Phase 7 availability_evidence")
        print("       and Phase 8/9 tactical_evidence, with full method provenance.")

    if result.availability:
        rule("AVAILABILITY EVIDENCE (Phase 7 mapping)")
        for draft in result.availability:
            print(json.dumps(draft.to_dict(), indent=2))
    if result.tactical:
        rule("TACTICAL EVIDENCE (Phase 8/9 mapping)")
        for tactical_draft in result.tactical:
            print(json.dumps(tactical_draft.to_dict(), indent=2))

    # -- 7. free-tier accounting -------------------------------------------
    rule("FREE-TIER USAGE")
    provenance = result.provenance
    kv("served from cache", provenance.from_cache)
    kv("routing strategy", provenance.routing_strategy or "(direct)")
    kv("prompt tokens", provenance.prompt_tokens)
    kv("completion tokens", provenance.completion_tokens)
    kv("max output tokens", provenance.max_output_tokens or settings.llm_max_output_tokens)
    kv("latency ms", provenance.latency_ms)
    # Phase 9.1.1 — landing exactly on the cap is a strong sign the generation
    # was truncated, which can silently cut a JSON envelope short. Flag it.
    max_tokens = provenance.max_output_tokens or settings.llm_max_output_tokens
    if (
        provenance.completion_tokens is not None
        and max_tokens is not None
        and provenance.completion_tokens >= max_tokens
    ):
        print(
            f"  ! WARNING: completion_tokens ({provenance.completion_tokens}) reached "
            f"max_output_tokens ({max_tokens}) — the response may have been truncated.\n"
        )
    cache = getattr(provider, "cache", None)
    if isinstance(cache, ResponseCache):
        kv("cache stats", json.dumps(cache.stats.to_dict()))
    budget = getattr(provider, "budget", None)
    if budget is not None:
        kv("call budget", json.dumps(budget.to_dict()))
    live_calls = getattr(provider, "live_calls", 0)
    kv("live API calls made", live_calls)
    if provider_name.is_real:
        print("\n  Re-running this exact command will hit the cache and cost 0 API calls.")
        print("  Use --no-cache to force a fresh call.")

    # -- 8. optional report -------------------------------------------------
    if args.json_out:
        report = {
            "settings": settings.describe(),
            "prompt_fingerprint": fingerprint.to_dict(),
            "temporal_class": item.temporal_class,
            "result": result.to_dict(),
            "provenance": provenance.to_dict(),
            "schema_problems": problems,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n  Report written to {args.json_out}")

    rule("RESULT")
    if problems:
        print("  FAILED — schema verification found problems (see above).")
        return EXIT_VERIFICATION
    if result.status is ExtractionStatus.PROVIDER_ERROR:
        print(f"  FAILED — provider error: {result.error}")
        return EXIT_PROVIDER
    if not result.ok:
        print(f"  FAILED — extraction status {result.status}: {result.error}")
        return EXIT_PROVIDER
    print(f"  PASSED — status {result.status}, {result.accepted_count} item(s) accepted.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
