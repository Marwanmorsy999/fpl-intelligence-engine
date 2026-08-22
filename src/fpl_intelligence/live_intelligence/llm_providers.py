"""Phase 9.1 real LLM providers — Gemini, Groq and OpenRouter.

Everything that is common to a live call — the cache lookup, the input-size
guard, the token cap, the pacing, the budget, the bounded retry, the token
accounting — lives once in :class:`RealLLMProvider`. A concrete provider
supplies only three things: how to build the HTTP request, how to read the text
out of the response, and how to recognise an error. That split matters because
the safeguards are the part that must not vary between providers: a guard that
one subclass forgets to apply is a guard that does not exist.

Ordering of the safeguards is itself a decision:

1. **Input guard** — reject an oversized prompt before anything is spent.
2. **Cache** — a hit costs nothing, so it is checked before the budget is
   consumed and before any pacing delay is served.
3. **Budget** — refuse the call if this process has already made its allotment.
4. **Rate limit** — sleep so the request stays inside the free-tier RPM.
5. **Request** — with a bounded retry that honours ``Retry-After``.
6. **Cache write** — so the next identical request skips steps 3–5 entirely.

Transport
---------

``httpx`` directly rather than each vendor's SDK: three SDKs would be three
dependency trees, three auth conventions and three retry policies competing
with the ones above. The HTTP shapes here are small and stable, and an injected
``http_client`` makes the whole path testable with
:class:`httpx.MockTransport` — which is how these classes are covered without
a single byte crossing the network in CI.
"""

from __future__ import annotations

import abc
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from fpl_intelligence.live_intelligence.extraction import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
)
from fpl_intelligence.live_intelligence.llm_settings import (
    API_KEY_ENV_VAR,
    DEFAULT_MODELS,
    LLMProviderName,
    LLMSettings,
)
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider
from fpl_intelligence.live_intelligence.prompt_registry import hash_input_text
from fpl_intelligence.live_intelligence.prompts import LLMPrompt
from fpl_intelligence.live_intelligence.rate_limit import (
    CallBudget,
    CallBudgetExceededError,
    RateLimiter,
)
from fpl_intelligence.live_intelligence.response_cache import (
    CacheEntry,
    NullResponseCache,
    ResponseCache,
    build_cache,
    make_cache_key,
)
from fpl_intelligence.live_intelligence.temporal_ledger import Clock, utc_now

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMAuthError(LLMProviderError):
    """The provider rejected the credential (401/403)."""


class LLMRateLimitError(LLMProviderError):
    """The provider returned 429 and the retry allowance was exhausted."""


class LLMResponseError(LLMProviderError):
    """The provider replied, but not with something we can read."""


class LLMModelNotAvailableError(LLMProviderError):
    """The provider returned a model-availability error (e.g. 404 model retired).

    This is a candidate for automatic fallback to another provider, because the
    failure is about the model identity, not the credential or the quota.
    """


class LLMBudgetError(LLMProviderError):
    """The local call budget refused this request before it was sent."""


class LLMInputTooLargeError(LLMProviderError):
    """The rendered prompt exceeded the configured input ceiling."""


#: Status codes worth one more attempt. 429 is included because a provider may
#: send a ``Retry-After`` that is shorter than the configured pacing interval.
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ProviderCompletion:
    """Normalised result of one successful provider call."""

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None

    def usage(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "finish_reason": self.finish_reason,
        }


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class RealLLMProvider(LLMProvider, abc.ABC):
    """Shared machinery for every network-backed provider.

    Args:
        api_key: Credential. Held in memory only; never logged or persisted.
        model: Provider-specific model id.
        max_output_tokens: Hard generation cap sent with every request. This is
            the single most important cost control: it bounds the worst case of
            a model that decides to keep talking.
        temperature: Decoding temperature. ``0.0`` by default, because
            extraction is a parsing task and reproducibility is what makes the
            response cache sound.
        timeout_seconds: Per-request HTTP timeout.
        max_input_chars: Reject a rendered prompt larger than this rather than
            paying to discover it was too large.
        cache: Response cache. Defaults to disabled so a caller must opt in
            explicitly to persistence.
        rate_limiter: Pacing guard. ``None`` means no pacing.
        budget: Per-process live-call ceiling. ``None`` means unlimited, which
            is only appropriate in tests.
        http_client: Injected client. Supplying one with a
            :class:`httpx.MockTransport` is how these providers are unit
            tested offline.
        max_retries: Additional attempts after the first failure.
        clock: Injected UTC clock for latency measurement.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_output_tokens: int,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
        max_input_chars: int = 12_000,
        cache: ResponseCache | None = None,
        rate_limiter: RateLimiter | None = None,
        budget: CallBudget | None = None,
        http_client: httpx.Client | None = None,
        max_retries: int = 2,
        clock: Clock = utc_now,
    ) -> None:
        if not api_key:
            raise LLMAuthError(
                f"{type(self).__name__} was constructed without an API key. "
                "Keys come from the environment only; see LLMSettings.require_api_key()."
            )
        self._api_key = api_key
        self._model = model
        self._max_output_tokens = int(max_output_tokens)
        self._temperature = float(temperature)
        self._timeout = float(timeout_seconds)
        self._max_input_chars = int(max_input_chars)
        self._cache: ResponseCache = cache if cache is not None else NullResponseCache()
        self._rate_limiter = rate_limiter
        self._budget = budget
        self._max_retries = max(0, int(max_retries))
        self._clock = clock
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self._timeout)
        #: Real HTTP requests issued to the provider, including retries and
        #: attempts that ultimately failed. This is what actually consumes a
        #: free-tier quota, so it is the number the budget is measured against.
        self.live_requests = 0
        #: Requests that returned a usable completion. Always <= live_requests.
        self.successful_live_completions = 0

    # -- LLMProvider --------------------------------------------------------

    @property
    def live_calls(self) -> int:
        """Live quota-consuming requests made by this instance.

        Alias for :attr:`live_requests`. Reported rather than
        :attr:`successful_live_completions` because a failed or retried request
        still consumed a provider request slot — under-reporting it would make
        the free-tier usage figure optimistic in exactly the situation where it
        matters most.
        """
        return self.live_requests

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def is_mock(self) -> bool:
        return False

    @property
    def max_output_tokens(self) -> int:
        return self._max_output_tokens

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def cache(self) -> ResponseCache:
        return self._cache

    @property
    def budget(self) -> CallBudget | None:
        return self._budget

    def close(self) -> None:
        """Close the HTTP client if this provider created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RealLLMProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- subclass contract --------------------------------------------------

    @abc.abstractmethod
    def _build_request(self, prompt: LLMPrompt) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Return ``(url, headers, json_body)`` for this prompt."""

    @abc.abstractmethod
    def _parse_response(self, payload: dict[str, Any]) -> ProviderCompletion:
        """Extract the completion text and usage from a decoded response body."""

    # -- the guarded call path ---------------------------------------------

    def complete(self, prompt: LLMPrompt) -> LLMResponse:
        rendered_chars = len(prompt.system) + len(prompt.user)
        if rendered_chars > self._max_input_chars:
            raise LLMInputTooLargeError(
                f"Rendered prompt is {rendered_chars} characters, over the "
                f"{self._max_input_chars} limit for provider '{self.provider_name}'. "
                "Split the source text or raise LLM_MAX_INPUT_CHARS deliberately; "
                "the cap exists so an accidental paste cannot consume a day's quota."
            )

        source_text = str(prompt.context.get("raw_text", prompt.user))
        cache_key = make_cache_key(
            provider_name=self.provider_name,
            model_name=self._model,
            prompt_hash=prompt.hash(),
            input_hash=hash_input_text(source_text),
            max_output_tokens=self._max_output_tokens,
            temperature=self._temperature,
        )

        cached = self._cache.get(cache_key)
        if cached is not None:
            usage = cached.usage or {}
            return LLMResponse(
                text=cached.response_text,
                provider_name=self.provider_name,
                model_name=self._model,
                is_mock=False,
                latency_ms=0,
                temperature=self._temperature,
                from_cache=True,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                max_output_tokens=self._max_output_tokens,
                finish_reason=usage.get("finish_reason"),
            )

        # Budget is NOT consumed here. A single logical completion can issue up
        # to ``max_retries + 1`` real HTTP requests, and every one of those
        # consumes a provider request slot. Charging one slot per ``complete()``
        # would under-count actual usage by up to that factor, so the budget is
        # claimed per HTTP attempt inside _invoke_with_retry().
        if self._rate_limiter is not None:
            self._rate_limiter.acquire()

        started = self._clock()
        completion = self._invoke_with_retry(prompt)
        latency_ms = int((self._clock() - started).total_seconds() * 1000)
        self.successful_live_completions += 1

        self._cache.put(
            CacheEntry(
                cache_key=cache_key,
                response_text=completion.text,
                provider_name=self.provider_name,
                model_name=self._model,
                prompt_hash=prompt.hash(),
                input_hash=hash_input_text(source_text),
                max_output_tokens=self._max_output_tokens,
                temperature=self._temperature,
                created_at=self._clock(),
                usage=completion.usage(),
            )
        )

        return LLMResponse(
            text=completion.text,
            provider_name=self.provider_name,
            model_name=self._model,
            is_mock=False,
            latency_ms=latency_ms,
            temperature=self._temperature,
            from_cache=False,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            max_output_tokens=self._max_output_tokens,
            finish_reason=completion.finish_reason,
        )

    # -- transport ----------------------------------------------------------

    def _consume_request_slot(self) -> None:
        """Claim budget for exactly one real HTTP request to the provider.

        Called immediately before every attempt, including retries, because a
        retried request consumes a provider request slot just like the first
        one. Raising here rather than after the request means an exhausted
        budget stops the call instead of reporting it after the quota is gone.
        """
        if self._budget is not None:
            try:
                self._budget.consume()
            except CallBudgetExceededError as exc:
                raise LLMBudgetError(str(exc)) from exc
        self.live_requests += 1

    def _invoke_with_retry(self, prompt: LLMPrompt) -> ProviderCompletion:
        url, headers, body = self._build_request(prompt)
        last_error: str = "no attempt was made"

        for attempt in range(self._max_retries + 1):
            self._consume_request_slot()
            try:
                response = self._client.post(url, headers=headers, json=body, timeout=self._timeout)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                if attempt >= self._max_retries:
                    raise LLMProviderError(f"{self.provider_name}: {last_error}") from exc
                self._backoff(attempt, None)
                continue

            if response.status_code in (401, 403):
                raise LLMAuthError(
                    f"{self.provider_name} rejected the credential "
                    f"(HTTP {response.status_code}). Check {self._api_key_env_var()} in "
                    f"your .env. Response: {_snippet(response.text)}"
                )

            if response.status_code in _RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}: {_snippet(response.text)}"
                if attempt >= self._max_retries:
                    if response.status_code == 429:
                        raise LLMRateLimitError(
                            f"{self.provider_name} free-tier rate limit hit and the retry "
                            f"allowance ({self._max_retries}) is exhausted. Increase "
                            "LLM_MIN_SECONDS_BETWEEN_CALLS, or wait for the quota window "
                            f"to reset. Last response: {last_error}"
                        )
                    raise LLMProviderError(f"{self.provider_name}: {last_error}")
                self._backoff(attempt, _retry_after_seconds(response))
                continue

            if response.status_code >= 400:
                if response.status_code == 404:
                    raise LLMModelNotAvailableError(
                        f"{self.provider_name} returned HTTP 404: {_snippet(response.text)}"
                    )
                raise LLMProviderError(
                    f"{self.provider_name} returned HTTP {response.status_code}: "
                    f"{_snippet(response.text)}"
                )

            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise LLMResponseError(
                    f"{self.provider_name} returned a non-JSON body: {_snippet(response.text)}"
                ) from exc

            if not isinstance(payload, dict):
                raise LLMResponseError(
                    f"{self.provider_name} returned a JSON {type(payload).__name__}, "
                    "expected an object."
                )
            return self._parse_response(payload)

        raise LLMProviderError(f"{self.provider_name}: {last_error}")

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        """Wait before retrying, preferring the provider's own instruction.

        The delay is floored at the configured pacing interval. Without that
        floor a provider answering 429 with ``Retry-After: 0`` (or any value
        below the interval) would be retried immediately, which both ignores
        the free-tier pacing guard and makes a 429 burst more likely — the
        opposite of what backing off is for.

        Capped at 60s: a longer wait is a quota window, not a transient blip,
        and blocking a script for minutes hides the real problem.
        """
        if retry_after is not None and retry_after > 0:
            delay = retry_after
        else:
            delay = min(2.0 * (2**attempt), 30.0)
        if self._rate_limiter is not None:
            delay = max(delay, self._rate_limiter.min_interval_seconds)
        delay = min(delay, 60.0)
        if self._rate_limiter is not None:
            self._rate_limiter.pause(delay)
        else:  # pragma: no cover - only reachable without pacing configured
            time.sleep(delay)

    def _api_key_env_var(self) -> str:
        try:
            return API_KEY_ENV_VAR[LLMProviderName(self.provider_name)]
        except ValueError:  # pragma: no cover - defensive
            return "the provider's API key variable"


def _snippet(text: str, limit: int = 300) -> str:
    """Truncate a provider error body. Never contains our credential."""
    cleaned = " ".join((text or "").split())
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` delay, treating non-positive values as absent.

    A literal ``Retry-After: 0`` must not be read as "retry now": returning
    ``0.0`` here would satisfy an ``is not None`` check downstream and produce a
    zero-delay retry. ``None`` means "no usable instruction", which lets the
    caller fall back to exponential backoff floored at the pacing interval.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------


class GeminiProvider(RealLLMProvider):
    """Google AI Studio (Gemini) via the ``generateContent`` REST endpoint.

    The system block is sent as ``system_instruction`` rather than prepended to
    the user turn, so the model treats the extraction contract as instructions
    rather than as data it might quote back.
    """

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, *, base_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._base_url = (base_url or self.BASE_URL).rstrip("/")

    @property
    def provider_name(self) -> str:
        return LLMProviderName.GEMINI.value

    def _build_request(self, prompt: LLMPrompt) -> tuple[str, dict[str, str], dict[str, Any]]:
        url = f"{self._base_url}/models/{self._model}:generateContent"
        headers = {
            "x-goog-api-key": self._api_key,
            "content-type": "application/json",
        }
        body: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": prompt.system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt.user}]}],
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": self._max_output_tokens,
                "candidateCount": 1,
                # Ask for JSON at the transport level as well as in the prompt.
                # Belt and braces: the engine still validates the payload.
                "responseMimeType": "application/json",
            },
        }
        return url, headers, body

    def _parse_response(self, payload: dict[str, Any]) -> ProviderCompletion:
        if "error" in payload:
            raise LLMResponseError(
                f"gemini error: {payload['error'].get('message', payload['error'])}"
            )
        candidates = payload.get("candidates") or []
        if not candidates:
            feedback = payload.get("promptFeedback") or {}
            raise LLMResponseError(
                "gemini returned no candidates "
                f"(promptFeedback={json.dumps(feedback)[:200]}). This usually means the "
                "prompt was blocked by a safety filter."
            )
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        finish_reason = candidate.get("finishReason")
        if not text.strip():
            raise LLMResponseError(
                f"gemini returned an empty completion (finishReason={finish_reason!r}). "
                "If this is MAX_TOKENS, raise LLM_MAX_OUTPUT_TOKENS; the cap is "
                "intentionally low to protect the free tier."
            )
        usage = payload.get("usageMetadata") or {}
        return ProviderCompletion(
            text=text,
            prompt_tokens=usage.get("promptTokenCount"),
            completion_tokens=usage.get("candidatesTokenCount"),
            finish_reason=finish_reason,
        )


# ---------------------------------------------------------------------------
# OpenAI-compatible chat completions (Groq, OpenRouter)
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider(RealLLMProvider, abc.ABC):
    """Shared implementation for ``/chat/completions`` style providers."""

    #: Set by subclasses.
    BASE_URL: str = ""
    #: Whether to request ``response_format={"type": "json_object"}``. Not every
    #: free model on every gateway supports it, so it is opt-out per provider.
    REQUEST_JSON_OBJECT: bool = True

    def __init__(self, *, base_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._base_url = (base_url or self.BASE_URL).rstrip("/")

    def _extra_headers(self) -> dict[str, str]:
        return {}

    def _build_request(self, prompt: LLMPrompt) -> tuple[str, dict[str, str], dict[str, Any]]:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers(),
        }
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
            "stream": False,
        }
        if self.REQUEST_JSON_OBJECT:
            body["response_format"] = {"type": "json_object"}
        return url, headers, body

    def _parse_response(self, payload: dict[str, Any]) -> ProviderCompletion:
        if payload.get("error"):
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise LLMResponseError(f"{self.provider_name} error: {message}")
        choices = payload.get("choices") or []
        if not choices:
            raise LLMResponseError(
                f"{self.provider_name} returned no choices: {json.dumps(payload)[:300]}"
            )
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        finish_reason = choice.get("finish_reason")
        if not text.strip():
            raise LLMResponseError(
                f"{self.provider_name} returned an empty completion "
                f"(finish_reason={finish_reason!r}). If this is 'length', raise "
                "LLM_MAX_OUTPUT_TOKENS; the cap is intentionally low."
            )
        usage = payload.get("usage") or {}
        return ProviderCompletion(
            text=text,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            finish_reason=finish_reason,
        )


class GroqProvider(OpenAICompatibleProvider):
    """Groq Cloud. OpenAI-compatible chat completions."""

    BASE_URL = "https://api.groq.com/openai/v1"

    @property
    def provider_name(self) -> str:
        return LLMProviderName.GROQ.value


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter gateway. OpenAI-compatible, with attribution headers.

    ``HTTP-Referer`` and ``X-Title`` are optional but requested by OpenRouter
    for free-tier attribution; both are non-secret and configurable.
    """

    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        *,
        referer: str = "https://github.com/fpl-intelligence-engine",
        title: str = "FPL Intelligence Engine (Phase 9.1)",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._referer = referer
        self._title = title

    @property
    def provider_name(self) -> str:
        return LLMProviderName.OPENROUTER.value

    def _extra_headers(self) -> dict[str, str]:
        return {"HTTP-Referer": self._referer, "X-Title": self._title}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


PROVIDER_CLASSES: dict[LLMProviderName, type[RealLLMProvider]] = {
    LLMProviderName.GEMINI: GeminiProvider,
    LLMProviderName.GROQ: GroqProvider,
    LLMProviderName.OPENROUTER: OpenRouterProvider,
}


class ProviderFactory:
    """Builds the configured provider, wired to the free-tier safeguards.

    The factory — not the caller — decides that a real provider always gets a
    cache, a rate limiter and a budget. Constructing a real provider without
    them is possible for tests, but it is not something ordinary application
    code can do by forgetting an argument.
    """

    def __init__(self, settings: LLMSettings) -> None:
        self._settings = settings

    @property
    def settings(self) -> LLMSettings:
        return self._settings

    def create(
        self,
        provider: LLMProviderName | str | None = None,
        *,
        cache: ResponseCache | None = None,
        rate_limiter: RateLimiter | None = None,
        budget: CallBudget | None = None,
        http_client: httpx.Client | None = None,
        mock_player_names: tuple[str, ...] = (),
    ) -> LLMProvider:
        """Instantiate a provider by name, defaulting to the configured one."""
        settings = self._settings
        target = LLMProviderName(provider) if provider is not None else settings.llm_provider

        if target is LLMProviderName.MOCK:
            return MockLLMProvider(
                player_names=mock_player_names,
                model_name=DEFAULT_MODELS[LLMProviderName.MOCK],
            )

        try:
            provider_cls = PROVIDER_CLASSES[target]
        except KeyError:  # pragma: no cover - LLMProviderName is exhaustive
            known = ", ".join(sorted(p.value for p in LLMProviderName))
            raise ValueError(f"Unknown provider '{target}'. Known: {known}") from None

        kwargs: dict[str, Any] = {
            "api_key": settings.require_api_key(target),
            "model": settings.model_for(target),
            "max_output_tokens": settings.llm_max_output_tokens,
            "temperature": settings.llm_temperature,
            "timeout_seconds": settings.llm_timeout_seconds,
            "max_input_chars": settings.llm_max_input_chars,
            "max_retries": settings.llm_max_retries,
            "cache": cache if cache is not None else self.default_cache(),
            "rate_limiter": (
                rate_limiter
                if rate_limiter is not None
                else RateLimiter(settings.llm_min_seconds_between_calls)
            ),
            "budget": budget if budget is not None else CallBudget(settings.llm_max_calls_per_run),
            "http_client": http_client,
        }
        if target is LLMProviderName.OPENROUTER:
            kwargs["referer"] = settings.openrouter_referer
            kwargs["title"] = settings.openrouter_title
        return provider_cls(**kwargs)

    def default_cache(self) -> ResponseCache:
        """Build the cache configured by settings.

        Public so a caller that builds several providers (e.g. ``ProviderRouter``)
        can create one cache and share it, instead of getting a fresh one per
        provider.
        """
        return build_cache(
            enabled=self._settings.llm_cache_enabled,
            path=self._settings.resolved_cache_path(),
        )


def build_provider(
    settings: LLMSettings,
    provider: LLMProviderName | str | None = None,
    **kwargs: Any,
) -> LLMProvider:
    """Module-level convenience wrapper around :class:`ProviderFactory`."""
    return ProviderFactory(settings).create(provider, **kwargs)
