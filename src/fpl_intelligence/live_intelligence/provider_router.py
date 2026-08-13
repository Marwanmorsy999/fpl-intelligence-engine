"""Phase 9.1 ProviderRouter — task-based routing, fallback, and round-robin.

The router decides *which* provider handles a given extraction task.
It is the central piece of the smart API-key/provider assignment feature:

1. **Task-based routing** — each extraction template is mapped to a
   preferred provider (e.g. availability → Groq for fast structured
   parsing, tactical → Gemini for longer context).
2. **Fallback** — if a provider returns a rate-limit/429 or an auth
   error, the router automatically retries the next provider in the
   fallback chain.
3. **Round-robin** — optional load balancing across providers for the
   same task, distributing quota evenly when multiple providers are
   configured.

The router never holds API keys itself; it delegates to the real
providers via :class:`ProviderFactory`, which reads credentials from
the environment only.

Routing metadata (``provider_name``, ``routing_strategy``,
``prompt_hash``) is persisted on every extraction run so that the
audit trail records *how* a result was produced, not just *what* it
contains.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fpl_intelligence.live_intelligence.extraction import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
)
from fpl_intelligence.live_intelligence.llm_providers import (
    LLMAuthError,
    LLMModelNotAvailableError,
    LLMRateLimitError,
    ProviderFactory,
)
from fpl_intelligence.live_intelligence.llm_settings import (
    API_KEY_ENV_VAR,
    LLMProviderName,
)
from fpl_intelligence.live_intelligence.prompts import PromptTemplate
from fpl_intelligence.live_intelligence.rate_limit import CallBudget, RateLimiter
from fpl_intelligence.live_intelligence.response_cache import ResponseCache


class RoutingStrategy(StrEnum):
    """How the router selected the provider for a given call."""

    TASK_BASED = "task_based"
    FALLBACK = "fallback"
    ROUND_ROBIN = "round_robin"


class ProviderRoutingError(LLMProviderError):
    """All providers in the fallback chain exhausted or none configured."""


@dataclass(frozen=True)
class RouteDecision:
    """The outcome of a routing decision, persisted for audit."""

    provider_name: str
    model_name: str
    routing_strategy: RoutingStrategy
    prompt_hash: str
    template_id: str
    task: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "routing_strategy": self.routing_strategy.value,
            "prompt_hash": self.prompt_hash,
            "template_id": self.template_id,
            "task": self.task,
        }


@dataclass(frozen=True)
class RouteFailure:
    """Why the primary provider's attempt failed (Phase 9.1.1 diagnostics).

    Carried so the dry-run can print *why* fallback happened: the primary
    provider that was attempted and a coarse failure reason. ``reason`` is one
    of ``rate_limit``, ``auth``, ``timeout``, ``schema_error`` or ``other``.
    Only the provider *name* is recorded here — never a credential.
    """

    primary_provider: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"primary_provider": self.primary_provider, "reason": self.reason}


class ProviderRouter(LLMProvider):
    """Routes extraction tasks to providers with fallback and round-robin.

    Implements :class:`LLMProvider` so it can be used as a drop-in
    provider in the extraction pipeline. When ``complete()`` is called,
    the router selects the appropriate provider, makes the call, and
    automatically retries on the next provider in the fallback chain
    if the first one returns a rate-limit or auth error.

    Args:
        factory: The :class:`ProviderFactory` used to build real providers.
        task_routes: Mapping from task name to the preferred provider.
            Defaults to the built-in task routing table.
        fallback_order: Ordered list of providers to try when the
            preferred provider fails. Defaults to all real providers
            in the order GROQ, GEMINI, OPENROUTER.
        round_robin: When True, the router cycles through providers
            for the same task instead of always using the preferred one.
        mock_player_names: Player names passed through to the mock
            provider when the router is configured for mock mode.
        budget: Shared call budget. Defaults to one built from settings.
        rate_limiter: Shared pacing guard. Defaults to one built from settings.
        cache: Shared response cache. Defaults to one built from settings.
    """

    def __init__(
        self,
        factory: ProviderFactory,
        task_routes: dict[str, LLMProviderName] | None = None,
        fallback_order: list[LLMProviderName] | None = None,
        round_robin: bool = False,
        mock_player_names: tuple[str, ...] = (),
        *,
        budget: CallBudget | None = None,
        rate_limiter: RateLimiter | None = None,
        cache: ResponseCache | None = None,
    ) -> None:
        self._factory = factory
        self._task_routes = task_routes or DEFAULT_TASK_ROUTES
        self._fallback_order = fallback_order or [
            LLMProviderName.GROQ,
            LLMProviderName.GEMINI,
            LLMProviderName.OPENROUTER,
        ]
        self._round_robin = round_robin
        self._mock_player_names = mock_player_names
        self._round_robin_counter = itertools.count()
        self._last_route: RouteDecision | None = None
        # One budget, one rate limiter and one cache for the router's entire
        # lifetime, shared by every provider it builds.
        #
        # This is load-bearing, not an optimisation. The router builds a fresh
        # provider per call and per fallback attempt, and ProviderFactory.create()
        # mints a *new* CallBudget and RateLimiter whenever it is not given one.
        # Left to the default, every routed call would therefore reset the
        # per-run ceiling (making LLM_MAX_CALLS_PER_RUN unenforceable) and start
        # with an empty pacing history (making every call look like a cold start
        # that never has to wait). Sharing them is what makes the free-tier
        # guards actually apply across routed, retried and fallback calls.
        settings = factory.settings
        self._budget = (
            budget if budget is not None else CallBudget(settings.llm_max_calls_per_run)
        )
        self._rate_limiter = (
            rate_limiter
            if rate_limiter is not None
            else RateLimiter(settings.llm_min_seconds_between_calls)
        )
        self._cache = cache if cache is not None else factory.default_cache()
        #: The routing decision for the most recent call, set even when the call
        #: fails. Used by the dry-run to report *which* provider was selected,
        #: not just which one succeeded.
        self._last_route_attempt: RouteDecision | None = None
        #: Live (non-cached) API calls made across every provider this router
        #: delegated to, including retries and fallbacks. Mirrors the
        #: ``live_calls`` attribute on :class:`RealLLMProvider` so a router,
        #: like a plain provider, can report its true free-tier usage.
        self.live_calls = 0
        #: When the most recent call needed fallback, details of the failed
        #: primary attempt. ``None`` when no fallback was required.
        self._last_failure: RouteFailure | None = None

    # -- shared safeguards -------------------------------------------------

    @property
    def budget(self) -> CallBudget:
        """The shared call budget, enforced across every routed call.

        Exposed under the same name a plain provider uses so the dry-run
        reports the router's real enforcement instead of finding nothing.
        """
        return self._budget

    @property
    def rate_limiter(self) -> RateLimiter:
        """The shared pacing guard, whose history persists across routed calls."""
        return self._rate_limiter

    @property
    def cache(self) -> ResponseCache:
        """The shared response cache. Cache hits never consume budget."""
        return self._cache

    # -- LLMProvider interface -------------------------------------------

    @property
    def provider_name(self) -> str:
        return "router"

    @property
    def model_name(self) -> str:
        return self._last_route.model_name if self._last_route else "router"

    @property
    def is_mock(self) -> bool:
        return False

    def complete(self, prompt: Any) -> LLMResponse:
        """Route the prompt to a provider with automatic fallback.

        The task is derived from the prompt's template_id:
        ``phase9.extract.availability`` → ``availability``,
        ``phase9.extract.tactical`` → ``tactical``,
        everything else → ``combined``.
        """
        task = _task_from_template(prompt.template_id)
        route = self._route(task, prompt)
        self._last_route_attempt = route
        provider = self._build_provider(route)

        try:
            response = provider.complete(prompt)
            self._absorb_live_calls(provider)
            self._last_route = route
            response = self._stamp(response, route)
            return response
        except LLMRateLimitError:
            self._absorb_live_calls(provider)
            self._record_failure(route, "rate_limit")
            self._safe_close(provider)
            return self._fallback(prompt, task, route, LLMRateLimitError)
        except LLMAuthError:
            self._absorb_live_calls(provider)
            self._record_failure(route, "auth")
            self._safe_close(provider)
            return self._fallback(prompt, task, route, LLMAuthError)
        except LLMModelNotAvailableError:
            self._absorb_live_calls(provider)
            self._record_failure(route, "model_unavailable")
            self._safe_close(provider)
            return self._fallback(prompt, task, route, LLMModelNotAvailableError)
        except LLMProviderError:
            self._absorb_live_calls(provider)
            self._safe_close(provider)
            raise

    def close(self) -> None:
        pass

    # -- routing ----------------------------------------------------------

    def route(
        self,
        task: str,
        template: PromptTemplate,
        *,
        prompt_hash: str,
        model_override: str | None = None,
    ) -> RouteDecision:
        """Pick a provider for *task* and return the routing decision.

        The decision includes the provider name, model name, strategy,
        and prompt hash — everything needed to persist provenance.
        """
        preferred = self._task_routes.get(task)
        if preferred is None:
            preferred = LLMProviderName.GROQ

        if self._round_robin:
            provider = self._next_round_robin()
            strategy = RoutingStrategy.ROUND_ROBIN
        else:
            provider = preferred
            strategy = RoutingStrategy.TASK_BASED

        # Verify the provider has a usable key; if not, try fallback.
        if not self._factory.settings.has_api_key(provider):
            return self._resolve_fallback(task, template, prompt_hash, model_override)

        model = self._factory.settings.model_for(provider)
        if model_override:
            model = model_override

        return RouteDecision(
            provider_name=provider.value,
            model_name=model,
            routing_strategy=strategy,
            prompt_hash=prompt_hash,
            template_id=template.template_id,
            task=task,
        )

    def _next_round_robin(self) -> LLMProviderName:
        """Return the next provider in the round-robin cycle."""
        providers = self._fallback_order
        idx = next(self._round_robin_counter) % len(providers)
        return providers[idx]

    def _resolve_fallback(
        self,
        task: str,
        template: PromptTemplate,
        prompt_hash: str,
        model_override: str | None = None,
    ) -> RouteDecision:
        """Try providers in fallback order until one has a usable key."""
        for provider in self._fallback_order:
            if self._factory.settings.has_api_key(provider):
                model = self._factory.settings.model_for(provider)
                if model_override:
                    model = model_override
                return RouteDecision(
                    provider_name=provider.value,
                    model_name=model,
                    routing_strategy=RoutingStrategy.FALLBACK,
                    prompt_hash=prompt_hash,
                    template_id=template.template_id,
                    task=task,
                )
        raise ProviderRoutingError(
            "No provider has a configured API key. Set at least one of "
            + ", ".join(API_KEY_ENV_VAR.values())
            + " in your .env file."
        )

    # -- internals --------------------------------------------------------

    def _route(self, task: str, prompt: Any) -> RouteDecision:
        """Build a RouteDecision for the given task and prompt."""
        template_id = prompt.template_id
        template = _resolve_template(template_id)
        return self.route(task, template, prompt_hash=prompt.hash())

    def _build_provider(self, route: RouteDecision) -> LLMProvider:
        """Build the provider described by *route*, wired to safeguards.

        The router's shared budget, rate limiter and cache are passed in
        explicitly. Omitting them would let the factory create per-provider
        instances and silently defeat both free-tier guards.
        """
        provider_name = LLMProviderName(route.provider_name)
        if provider_name is LLMProviderName.MOCK:
            from fpl_intelligence.live_intelligence.llm_settings import DEFAULT_MODELS
            from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider

            return MockLLMProvider(
                player_names=self._mock_player_names,
                model_name=DEFAULT_MODELS[LLMProviderName.MOCK],
            )
        return self._factory.create(
            provider=provider_name,
            cache=self._cache,
            rate_limiter=self._rate_limiter,
            budget=self._budget,
        )

    def _fallback(
        self,
        prompt: Any,
        task: str,
        failed_route: RouteDecision,
        original_error: type[LLMProviderError],
    ) -> LLMResponse:
        """Try providers in fallback order after a failure."""
        tried = {LLMProviderName(failed_route.provider_name)}

        for fallback_provider in self._fallback_order:
            if fallback_provider in tried:
                continue
            if not self._factory.settings.has_api_key(fallback_provider):
                continue

            tried.add(fallback_provider)
            route = RouteDecision(
                provider_name=fallback_provider.value,
                model_name=self._factory.settings.model_for(fallback_provider),
                routing_strategy=RoutingStrategy.FALLBACK,
                prompt_hash=failed_route.prompt_hash,
                template_id=failed_route.template_id,
                task=task,
            )
            provider = self._build_provider(route)

            try:
                response = provider.complete(prompt)
                self._absorb_live_calls(provider)
                self._last_route = route
                self._safe_close(provider)
                return self._stamp(response, route)
            except LLMProviderError:
                self._absorb_live_calls(provider)
                self._safe_close(provider)
                continue

        raise ProviderRoutingError(
            f"All providers failed for task '{task}' after trying "
            f"{len(tried)} provider(s). Original error: {original_error.__name__}"
        )

    def _absorb_live_calls(self, provider: LLMProvider) -> None:
        """Fold a delegated provider's live-call count into the router's.

        Every real provider each route builds has its own ``live_calls``
        counter. The router sums them so free-tier usage is reported honestly
        even when several providers are tried (task-based + retries + fallback).
        Mock providers report no ``live_calls``, which is correct.
        """
        self.live_calls += int(getattr(provider, "live_calls", 0) or 0)

    def _record_failure(self, route: RouteDecision, reason: str) -> None:
        """Record why the primary attempt failed, for dry-run diagnostics.

        Only the provider name and a coarse reason are kept — never a secret,
        never a response body.
        """
        self._last_failure = RouteFailure(primary_provider=route.provider_name, reason=reason)

    @staticmethod
    def _safe_close(provider: LLMProvider) -> None:
        """Close a provider if it supports it (mock providers may not)."""
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _stamp(response: LLMResponse, route: RouteDecision) -> LLMResponse:
        """Attach the routing strategy (and actual provider identity) to a response.

        Called on every successfully returned response regardless of whether it
        came via task routing, round-robin or fallback, so the response's
        ``routing_strategy`` always reflects how the provider was reached.
        """
        if response.routing_strategy != "":
            return response
        return LLMResponse(
            text=response.text,
            provider_name=response.provider_name,
            model_name=response.model_name,
            is_mock=response.is_mock,
            latency_ms=response.latency_ms,
            temperature=response.temperature,
            from_cache=response.from_cache,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            max_output_tokens=response.max_output_tokens,
            finish_reason=response.finish_reason,
            routing_strategy=route.routing_strategy.value,
        )

    @property
    def last_route(self) -> RouteDecision | None:
        """The most recent *successful* routing decision, for audit/persistence."""
        return self._last_route

    @property
    def last_route_attempt(self) -> RouteDecision | None:
        """The most recent routing decision, even if the call failed.

        Unlike :attr:`last_route`, this is set before the provider call so the
        dry-run can report *which* provider was selected even when the call
        fails before a fallback succeeds.
        """
        return self._last_route_attempt

    @property
    def last_failure(self) -> RouteFailure | None:
        """Details of the most recent failed primary attempt, if any.

        ``None`` when the last call succeeded on the first provider (no
        fallback was needed). Used by the dry-run to print primary provider and
        failure reason when ``routing_strategy`` is ``fallback``.
        """
        return self._last_failure


def _task_from_template(template_id: str) -> str:
    """Derive the extraction task from a template id."""
    if "availability" in template_id:
        return "availability"
    if "tactical" in template_id:
        return "tactical"
    return "combined"


def _resolve_template(template_id: str) -> PromptTemplate:
    """Resolve a template by id, raising a clear error for unknown ids."""
    from fpl_intelligence.live_intelligence.prompts import get_template

    return get_template(template_id)


#: Default task-to-provider mapping. Availability extraction prefers Groq
#: for fast structured JSON parsing; tactical extraction prefers Gemini
#: for its longer context window; OpenRouter acts as a general fallback.
DEFAULT_TASK_ROUTES: dict[str, LLMProviderName] = {
    "availability": LLMProviderName.GROQ,
    "tactical": LLMProviderName.GEMINI,
    "combined": LLMProviderName.GEMINI,
}
