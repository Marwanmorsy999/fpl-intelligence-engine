"""Regression tests for the seven Phase 9.1 code-review findings (1–7).

Each test targets one finding the review flagged and that we remediated. They
run fully offline: no network, no API keys, no live database.

Findings covered
----------------
1. ProviderRouter now shares ONE budget/rate-limiter/cache across every built
   provider (free-tier caps were previously unenforceable across routed calls).
2. MockLLM ``"available"`` must not fire inside ``"unavailable"``.
3. Corroboration ranks an explicit START above a vague AVAILABLE.
4. ``LLMSettings.model_for`` global ``LLM_MODEL`` override must not leak onto
   fallback providers.
5. The live-call budget is charged per real HTTP request, including retries.
6. Historical FPL ``"a"`` status code maps to AVAILABLE (not START) — aligned
   with the event-type mapping.
7. ``Retry-After: 0`` (and any non-positive value) is treated as absent, and
   backoff is floored at the pacing interval.
"""

from __future__ import annotations

import json

import httpx
import pytest

from fpl_intelligence.availability.evidence import corroborate
from fpl_intelligence.availability.historical.providers import _map_fpl_status
from fpl_intelligence.availability.models import AvailabilityStatus
from fpl_intelligence.live_intelligence.llm_providers import (
    LLMBudgetError,
    LLMRateLimitError,
    ProviderCompletion,
    RealLLMProvider,
    _retry_after_seconds,
)
from fpl_intelligence.live_intelligence.llm_settings import (
    DEFAULT_MODELS,
    LLMProviderName,
    LLMSettings,
)
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider
from fpl_intelligence.live_intelligence.prompts import LLMPrompt
from fpl_intelligence.live_intelligence.provider_router import ProviderRouter
from fpl_intelligence.live_intelligence.rate_limit import CallBudget, RateLimiter
from fpl_intelligence.live_intelligence.response_cache import NullResponseCache

# ---------------------------------------------------------------------------
# Shared offline doubles
# ---------------------------------------------------------------------------


class _Settings:
    """Minimal settings double for the router's ``factory.settings``."""

    def __init__(self, configured: set[LLMProviderName]) -> None:
        self._configured = set(configured)
        self.llm_max_calls_per_run = 8
        self.llm_min_seconds_between_calls = 0.0

    def has_api_key(self, provider: LLMProviderName) -> bool:
        return provider in self._configured

    def model_for(self, provider: LLMProviderName) -> str:
        return f"fake-{provider.value}"


class _CapturingFactory:
    """ProviderFactory double that records the kwargs passed to ``create``."""

    def __init__(
        self,
        configured: set[LLMProviderName],
        providers: dict[LLMProviderName, object] | None = None,
    ) -> None:
        self.settings = _Settings(configured)
        self._providers = providers or {}
        self.created: list[tuple[LLMProviderName, dict[str, object]]] = []

    def create(self, provider: LLMProviderName | str, **kwargs: object) -> object:
        provider = LLMProviderName(provider)
        self.created.append((provider, kwargs))
        return self._providers.get(provider) or MockLLMProvider(player_names=("X",))


class _FailingProvider:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def complete(self, prompt: object) -> None:
        raise self._exc

    def close(self) -> None:
        pass


class _RouterPrompt:
    def __init__(self, template_id: str) -> None:
        self.template_id = template_id
        self.context: dict[str, object] = {"raw_text": "", "team_hint": None}

    def hash(self) -> str:
        return f"h-{self.template_id}"


class _TestRealProvider(RealLLMProvider):
    """Concrete RealLLMProvider for offline transport tests."""

    @property
    def provider_name(self) -> str:
        return LLMProviderName.GROQ.value

    def _build_request(self, prompt: LLMPrompt) -> tuple[str, dict[str, str], dict[str, object]]:
        return "https://example.test/v1/complete", {"x-key": "k"}, {"u": prompt.user}

    def _parse_response(self, payload: dict[str, object]) -> ProviderCompletion:
        return ProviderCompletion(text=str(payload.get("text", "")))


def _mock_prompt(text: str) -> LLMPrompt:
    return LLMPrompt(
        template_id="phase9.extract.availability",
        version="1",
        system="s",
        user=text,
        schema_version="1",
        context={"raw_text": text, "team_hint": None},
    )


def _no_sleep(s: float) -> None:  # pragma: no cover - test hook
    return None


# ---------------------------------------------------------------------------
# Finding 1 — router shares one budget/limiter/cache across built providers
# ---------------------------------------------------------------------------


def test_router_passes_shared_safeguards_to_primary_provider() -> None:
    shared_budget = CallBudget(5)
    shared_limiter = RateLimiter(0.0, sleep=_no_sleep)
    shared_cache = NullResponseCache()
    factory = _CapturingFactory({LLMProviderName.GROQ})
    router = ProviderRouter(
        factory,
        budget=shared_budget,
        rate_limiter=shared_limiter,
        cache=shared_cache,
    )

    router.complete(_RouterPrompt("phase9.extract.availability"))

    # The router exposes the same objects it built its providers from.
    assert router.budget is shared_budget
    assert router.rate_limiter is shared_limiter
    assert router.cache is shared_cache

    # The single primary provider received the shared instances by reference.
    assert len(factory.created) == 1
    kwargs = factory.created[0][1]
    assert kwargs["budget"] is shared_budget
    assert kwargs["rate_limiter"] is shared_limiter
    assert kwargs["cache"] is shared_cache


def test_router_does_not_reset_safeguards_between_primary_and_fallback() -> None:
    shared_budget = CallBudget(5)
    shared_limiter = RateLimiter(0.0, sleep=_no_sleep)
    shared_cache = NullResponseCache()
    factory = _CapturingFactory(
        {LLMProviderName.GROQ, LLMProviderName.GEMINI},
        providers={LLMProviderName.GROQ: _FailingProvider(LLMRateLimitError("429"))},
    )
    router = ProviderRouter(
        factory,
        budget=shared_budget,
        rate_limiter=shared_limiter,
        cache=shared_cache,
    )

    router.complete(_RouterPrompt("phase9.extract.availability"))

    # Primary + successful fallback must have received the SAME budget/limiter/
    # cache — otherwise a fresh budget per call would let free-tier caps reset.
    assert len(factory.created) == 2
    budgets = [kw["budget"] for _, kw in factory.created]
    limiters = [kw["rate_limiter"] for _, kw in factory.created]
    caches = [kw["cache"] for _, kw in factory.created]
    assert budgets[0] is budgets[1] is shared_budget
    assert limiters[0] is limiters[1] is shared_limiter
    assert caches[0] is caches[1] is shared_cache


# ---------------------------------------------------------------------------
# Finding 2 — "unavailable" must not match the "available" mock rule
# ---------------------------------------------------------------------------


def test_unavailable_maps_to_doubtful_not_available() -> None:
    provider = MockLLMProvider(player_names=("Haaland",))
    data = json.loads(provider.complete(_mock_prompt("Haaland is unavailable")).text)
    statuses = [
        e["status_mentioned"]
        for e in data["availability_evidence"]
        if e["player_name"] == "Haaland"
    ]
    assert AvailabilityStatus.AVAILABLE not in statuses
    assert AvailabilityStatus.DOUBTFUL in statuses


def test_available_still_matches_its_own_rule() -> None:
    provider = MockLLMProvider(player_names=("Haaland",))
    data = json.loads(provider.complete(_mock_prompt("Haaland is available")).text)
    statuses = [
        e["status_mentioned"]
        for e in data["availability_evidence"]
        if e["player_name"] == "Haaland"
    ]
    assert statuses == [AvailabilityStatus.AVAILABLE]


# ---------------------------------------------------------------------------
# Finding 3 — START ranks above AVAILABLE in corroboration
# ---------------------------------------------------------------------------


def test_start_overrides_available_in_corroboration() -> None:
    items = [
        {
            "reliability": "reliable_journalist",
            "evidence_type": "lineup_hint",
            "status_mentioned": AvailabilityStatus.AVAILABLE,
            "published_at": "2025-08-10T10:00:00+00:00",
            "source_name": "A",
        },
        {
            "reliability": "reliable_journalist",
            "evidence_type": "lineup_hint",
            "status_mentioned": AvailabilityStatus.START,
            "published_at": "2025-08-10T11:00:00+00:00",
            "source_name": "B",
        },
    ]
    result = corroborate(items)
    assert result["status"] == AvailabilityStatus.START


# ---------------------------------------------------------------------------
# Finding 4 — LLM_MODEL override must not leak onto fallback providers
# ---------------------------------------------------------------------------


def test_llm_model_override_only_applies_to_primary() -> None:
    settings = LLMSettings(
        llm_provider="groq",
        llm_model="override-model",
        groq_api_key="g",
        gemini_api_key="m",
    )
    assert settings.model_for(LLMProviderName.GROQ) == "override-model"
    assert settings.model_for(LLMProviderName.GEMINI) == DEFAULT_MODELS[LLMProviderName.GEMINI]
    # Unspecified target means the primary provider → override applies.
    assert settings.model_for() == "override-model"


def test_no_override_uses_provider_defaults() -> None:
    settings = LLMSettings(llm_provider="groq", groq_api_key="g", gemini_api_key="m")
    assert settings.model_for(LLMProviderName.GROQ) == DEFAULT_MODELS[LLMProviderName.GROQ]
    assert settings.model_for(LLMProviderName.GEMINI) == DEFAULT_MODELS[LLMProviderName.GEMINI]


# ---------------------------------------------------------------------------
# Finding 5 — budget is charged per real HTTP request (including retries)
# ---------------------------------------------------------------------------


def _transport(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_budget_charged_once_per_http_attempt_including_retries() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"text": "ok"})

    budget = CallBudget(5)
    provider = _TestRealProvider(
        api_key="k",
        model="m",
        max_output_tokens=10,
        http_client=_transport(handler),
        max_retries=2,
        budget=budget,
        # A no-op limiter avoids real sleeps while still exercising the path.
        rate_limiter=RateLimiter(0.0, sleep=_no_sleep),
    )

    response = provider.complete(_mock_prompt("Haaland is available"))
    assert response.text == "ok"
    # 429, 429, then 200 = 3 HTTP attempts, each consuming one budget slot.
    assert provider.live_requests == 3
    assert provider.successful_live_completions == 1
    assert budget.used == 3


def test_budget_exhaustion_raises_before_sending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "ok"})

    budget = CallBudget(1)
    provider = _TestRealProvider(
        api_key="k",
        model="m",
        max_output_tokens=10,
        http_client=_transport(handler),
        max_retries=0,
        budget=budget,
        rate_limiter=None,
    )

    provider.complete(_mock_prompt("x"))
    assert budget.used == 1
    with pytest.raises(LLMBudgetError):
        provider.complete(_mock_prompt("x"))
    # The refused call must not have advanced the budget.
    assert budget.used == 1


# ---------------------------------------------------------------------------
# Finding 6 — historical FPL "a" code maps to AVAILABLE (not START)
# ---------------------------------------------------------------------------


def test_fpl_status_a_maps_to_available() -> None:
    assert _map_fpl_status("a") == AvailabilityStatus.AVAILABLE


# ---------------------------------------------------------------------------
# Finding 7 — Retry-After <= 0 is absent; backoff floors at pacing interval
# ---------------------------------------------------------------------------


def test_retry_after_zero_treated_as_absent() -> None:
    assert _retry_after_seconds(httpx.Response(429, headers={"retry-after": "0"})) is None
    assert _retry_after_seconds(httpx.Response(429, headers={"retry-after": "abc"})) is None
    assert _retry_after_seconds(httpx.Response(200)) is None
    assert _retry_after_seconds(httpx.Response(429, headers={"retry-after": "5"})) == 5.0


def test_backoff_floors_delay_at_pacing_interval() -> None:
    sleeps: list[float] = []
    limiter = RateLimiter(10.0, sleep=lambda s: sleeps.append(s))
    provider = _TestRealProvider(
        api_key="k",
        model="m",
        max_output_tokens=10,
        rate_limiter=limiter,
        budget=CallBudget(5),
    )
    # No Retry-After → exponential would be 2.0; the 10s pacing floor wins.
    provider._backoff(attempt=0, retry_after=None)
    assert sleeps == [10.0]


def test_backoff_honours_positive_retry_after() -> None:
    sleeps: list[float] = []
    limiter = RateLimiter(0.0, sleep=lambda s: sleeps.append(s))
    provider = _TestRealProvider(
        api_key="k",
        model="m",
        max_output_tokens=10,
        rate_limiter=limiter,
        budget=CallBudget(5),
    )
    provider._backoff(attempt=0, retry_after=3.0)
    assert sleeps == [3.0]
