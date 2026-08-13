"""Phase 9.1 secure runtime configuration for real LLM providers.

Secrets enter the process from exactly one place: the environment (normally
populated from a local, git-ignored ``.env``). No API key is ever written into
source, into a prompt, into a log line, into the response cache, or into the
database. :class:`LLMSettings` holds keys as :class:`~pydantic.SecretStr`, and
:meth:`LLMSettings.describe` — the only representation intended for printing —
emits a redacted fingerprint rather than the key.

Free-tier posture
-----------------

The defaults here are deliberately *pessimistic*. Every knob that could cost
money or trip a free-tier quota starts at a conservative value and has to be
raised explicitly:

===============================  =======  ==================================
Setting                          Default  Why
===============================  =======  ==================================
``llm_provider``                 ``mock`` A real call is never the default.
``llm_max_output_tokens``        ``2048`` Caps runaway generation per call.
``llm_max_input_chars``          ``12000``Caps prompt size before it is sent.
``llm_min_seconds_between_calls````6.0``  ~10 requests/min, under most free tiers.
``llm_max_calls_per_run``        ``8``    Hard ceiling on a single process.
``llm_max_retries``              ``2``    Bounded backoff; never hammers a 429.
``llm_cache_enabled``            ``True`` Identical request → zero API calls.
===============================  =======  ==================================

Graceful failure
----------------

A live run that cannot find its configuration must say so in one clear
sentence, not surface a ``None`` five frames later. :class:`MissingEnvFileError`
and :class:`MissingAPIKeyError` both carry the exact variable name, the file
that was searched, and the remedy.
"""
from __future__ import annotations

import hashlib
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Provider identity
# ---------------------------------------------------------------------------


class LLMProviderName(StrEnum):
    """Providers Phase 9.1 knows how to build."""

    MOCK = "mock"
    GEMINI = "gemini"
    GROQ = "groq"
    OPENROUTER = "openrouter"

    @property
    def is_real(self) -> bool:
        """True for providers that perform network calls and cost quota."""
        return self is not LLMProviderName.MOCK


#: Environment variable that supplies each real provider's credential. These
#: names are the contract with the user's ``.env``; they are never defaulted to
#: a literal value.
API_KEY_ENV_VAR: dict[LLMProviderName, str] = {
    LLMProviderName.GEMINI: "GOOGLE_API_KEY",
    LLMProviderName.GROQ: "GROQ_API_KEY",
    LLMProviderName.OPENROUTER: "OPENROUTER_API_KEY",
}

#: Default model per provider. Chosen to sit inside the free tier of each
#: service at the time of writing; override with ``LLM_MODEL``.
DEFAULT_MODELS: dict[LLMProviderName, str] = {
    LLMProviderName.MOCK: "mock-deterministic-v1",
    LLMProviderName.GEMINI: "gemini-2.5-flash",
    LLMProviderName.GROQ: "llama-3.3-70b-versatile",
    LLMProviderName.OPENROUTER: "meta-llama/llama-3.3-70b-instruct:free",
}

#: Free-tier guardrail defaults, referenced by the docs and the dry-run banner.
#: ``llm_max_output_tokens`` is set to 2048 (Phase 9.1.1): structured extraction
#: envelopes routinely need well over 1024 completion tokens, and the cost
#: control that matters is the warning when a run lands exactly on the cap.
FREE_TIER_MAX_OUTPUT_TOKENS = 2048
FREE_TIER_MAX_INPUT_CHARS = 12_000
FREE_TIER_MIN_INTERVAL_SECONDS = 6.0
FREE_TIER_MAX_CALLS_PER_RUN = 8
FREE_TIER_MAX_RETRIES = 2


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMSettingsError(RuntimeError):
    """Base class for configuration problems that block a live run."""


class MissingEnvFileError(LLMSettingsError):
    """Raised when a live run is requested but no ``.env`` could be found."""


class MissingAPIKeyError(LLMSettingsError):
    """Raised when the selected provider has no credential configured."""


# ---------------------------------------------------------------------------
# .env discovery
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """Repository root, derived from this file's location.

    ``src/fpl_intelligence/live_intelligence/llm_settings.py`` → three parents
    up is the checkout root. Used so a script run from any working directory
    still finds the project's ``.env``.
    """
    return Path(__file__).resolve().parents[3]


def find_env_file(start: Path | str | None = None) -> Path | None:
    """Return the nearest ``.env``, searching ``start`` upwards then the repo root.

    Returns ``None`` rather than raising: whether a missing ``.env`` is fatal
    depends on whether the caller intends to make live calls, and that decision
    belongs to :func:`load_llm_settings`.
    """
    candidates: list[Path] = []
    base = Path(start).resolve() if start is not None else Path.cwd().resolve()
    candidates.extend([base, *base.parents])
    root = repo_root()
    if root not in candidates:
        candidates.append(root)

    for directory in candidates:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def _fingerprint(secret: str) -> str:
    """Short, non-reversible identifier for a credential.

    Enough to confirm "the key I think I loaded is the key that was used" in a
    log or a bug report, useless to anyone who intercepts it.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class LLMSettings(BaseSettings):
    """Runtime configuration for the Phase 9.1 LLM layer.

    Every field is populated from the process environment (case-insensitively),
    which ``pydantic-settings`` seeds from ``.env`` when one is supplied. The
    class never reads a hardcoded credential and has no default for any key.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- credentials (never defaulted, never logged) ------------------------
    google_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None

    # -- provider selection -------------------------------------------------
    llm_provider: LLMProviderName = LLMProviderName.MOCK
    #: Explicit model override. ``None`` means "use this provider's default".
    llm_model: str | None = None

    # -- free-tier safeguards ----------------------------------------------
    llm_max_output_tokens: int = Field(default=FREE_TIER_MAX_OUTPUT_TOKENS, ge=1, le=8192)
    llm_max_input_chars: int = Field(default=FREE_TIER_MAX_INPUT_CHARS, ge=100, le=200_000)
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    llm_min_seconds_between_calls: float = Field(
        default=FREE_TIER_MIN_INTERVAL_SECONDS, ge=0.0, le=600.0
    )
    llm_max_calls_per_run: int = Field(default=FREE_TIER_MAX_CALLS_PER_RUN, ge=1, le=1000)
    llm_max_retries: int = Field(default=FREE_TIER_MAX_RETRIES, ge=0, le=5)

    # -- response cache -----------------------------------------------------
    llm_cache_enabled: bool = True
    llm_cache_path: Path = Path("data/cache/llm_response_cache.sqlite")

    # -- OpenRouter attribution headers (optional, non-secret) --------------
    openrouter_referer: str = "https://github.com/fpl-intelligence-engine"
    openrouter_title: str = "FPL Intelligence Engine (Phase 9.1)"

    @field_validator("llm_model")
    @classmethod
    def _blank_model_is_none(cls, value: str | None) -> str | None:
        """Treat ``LLM_MODEL=`` in a ``.env`` as "unset", not as an empty model."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    # -- derived accessors --------------------------------------------------

    @property
    def provider(self) -> LLMProviderName:
        return self.llm_provider

    def model_for(self, provider: LLMProviderName | None = None) -> str:
        """Model id for ``provider``, honouring an explicit ``LLM_MODEL`` override.

        The ``LLM_MODEL`` env var is a shortcut for pinning the *primary*
        provider's model. It must not leak onto fallback providers: a GEMINI
        override applied to a GROQ fallback request would point GROQ at a model
        that only exists on GEMINI, producing a 404 far from the cause. The
        override therefore applies only when ``provider`` is the configured
        primary (or unspecified, which also means the primary).
        """
        target = provider or self.llm_provider
        if self.llm_model and target == self.llm_provider:
            return self.llm_model
        return DEFAULT_MODELS[target]

    def raw_api_key(self, provider: LLMProviderName | None = None) -> str | None:
        """Return the configured key for ``provider``, or ``None`` if unset."""
        target = provider or self.llm_provider
        secret: SecretStr | None = {
            LLMProviderName.GEMINI: self.google_api_key,
            LLMProviderName.GROQ: self.groq_api_key,
            LLMProviderName.OPENROUTER: self.openrouter_api_key,
        }.get(target)
        if secret is None:
            return None
        value = secret.get_secret_value().strip()
        return value or None

    def has_api_key(self, provider: LLMProviderName | None = None) -> bool:
        target = provider or self.llm_provider
        if not target.is_real:
            return True
        return self.raw_api_key(target) is not None

    def require_api_key(self, provider: LLMProviderName | None = None) -> str:
        """Return the key for ``provider`` or raise a remediable error.

        Failing here — loudly, with the variable name — is the whole point: a
        live run that silently degrades to an unauthenticated request produces
        a confusing 401 far from its cause.
        """
        target = provider or self.llm_provider
        if not target.is_real:
            raise MissingAPIKeyError(
                f"Provider '{target}' is a test double and has no API key. "
                "Requesting a credential for it is a programming error."
            )
        key = self.raw_api_key(target)
        if key:
            return key
        var = API_KEY_ENV_VAR[target]
        raise MissingAPIKeyError(
            f"No API key for provider '{target}'. Set {var} in your local .env "
            f"(or export it in the environment) and re-run. The repository never "
            f"stores credentials: .env is git-ignored and no key is hardcoded. "
            f"Searched .env location: {find_env_file() or '<none found>'}"
        )

    def configured_providers(self) -> list[LLMProviderName]:
        """Real providers that currently have a usable credential."""
        return [p for p in API_KEY_ENV_VAR if self.raw_api_key(p) is not None]

    def resolved_cache_path(self) -> Path:
        """Absolute cache path, anchored at the repo root when relative."""
        path = Path(self.llm_cache_path)
        return path if path.is_absolute() else (repo_root() / path)

    def describe(self) -> dict[str, Any]:
        """Redacted, printable summary. The only sanctioned way to log settings.

        Credentials appear as ``sha256:<12 hex>`` fingerprints so a run can be
        correlated with a key without the key ever reaching a terminal, a log
        file, or a bug report.
        """
        keys: dict[str, str] = {}
        for provider, var in API_KEY_ENV_VAR.items():
            raw = self.raw_api_key(provider)
            keys[var] = f"sha256:{_fingerprint(raw)}" if raw else "<not set>"
        return {
            "provider": str(self.llm_provider),
            "model": self.model_for(),
            "api_keys": keys,
            "max_output_tokens": self.llm_max_output_tokens,
            "max_input_chars": self.llm_max_input_chars,
            "temperature": self.llm_temperature,
            "timeout_seconds": self.llm_timeout_seconds,
            "min_seconds_between_calls": self.llm_min_seconds_between_calls,
            "max_calls_per_run": self.llm_max_calls_per_run,
            "max_retries": self.llm_max_retries,
            "cache_enabled": self.llm_cache_enabled,
            "cache_path": str(self.resolved_cache_path()),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


_UNSET = object()


def load_llm_settings(
    *,
    env_file: Path | str | None | object = _UNSET,
    require_env_file: bool = False,
    **overrides: Any,
) -> LLMSettings:
    """Build :class:`LLMSettings`, optionally insisting that a ``.env`` exists.

    Args:
        env_file: Explicit ``.env`` path. Omit to auto-discover from the current
            working directory upwards (then the repo root). Pass ``None`` to
            read the process environment only — the form tests use, so a
            developer's real ``.env`` can never leak into a unit test.
        require_env_file: When True (live runs), a missing ``.env`` is a hard,
            explanatory failure rather than a silent fallback to defaults.
        **overrides: Direct field overrides, highest precedence. Used by the
            dry-run script's command-line flags.

    Raises:
        MissingEnvFileError: ``require_env_file`` is set and no file was found.
    """
    resolved: Path | None
    if env_file is _UNSET:
        resolved = find_env_file()
    elif env_file is None:
        resolved = None
    else:
        resolved = Path(env_file)  # type: ignore[arg-type]
        if require_env_file and not resolved.is_file():
            raise MissingEnvFileError(_missing_env_message(resolved))

    if require_env_file and resolved is None:
        raise MissingEnvFileError(_missing_env_message(repo_root() / ".env"))

    return LLMSettings(_env_file=resolved, **overrides)  # type: ignore[call-arg]


def _missing_env_message(expected: Path) -> str:
    variables = "\n".join(f"  {var}=<your-key>" for var in API_KEY_ENV_VAR.values())
    return (
        f"No .env file found (looked for {expected}).\n"
        "A live LLM run needs credentials, and this project never hardcodes them.\n"
        "Create the file with at least one of:\n"
        f"{variables}\n"
        "The file is already listed in .gitignore, so it will not be committed.\n"
        "Alternatively export the variables in your shell, or run with "
        "--provider mock to exercise the pipeline offline."
    )


@lru_cache(maxsize=1)
def get_llm_settings() -> LLMSettings:
    """Process-wide cached settings for ordinary application code."""
    return load_llm_settings()


def reset_llm_settings_cache() -> None:
    """Clear the cache. Tests use this; application code should not need it."""
    get_llm_settings.cache_clear()
