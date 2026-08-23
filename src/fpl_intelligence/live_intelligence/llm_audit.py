"""Phase 20.4 — production LLM model audit.

Before the assistant trusts a provider it asks that provider, right now:
"which models do you actually serve?" (each provider's public ``models``
endpoint, 8 s timeout). The configured default is kept only when the provider
still lists it; otherwise the audit picks a currently-valid id from a small
preference list and logs the exact error for the retired one.

The audit result is cached in-process for 10 minutes so the brief path pays
the probe at most once per warm instance.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from fpl_intelligence.live_intelligence.llm_settings import DEFAULT_MODELS, LLMProviderName

logger = logging.getLogger(__name__)

AUDIT_TIMEOUT_SECONDS = 8.0
AUDIT_CACHE_TTL = 600.0

#: Fallback preference per provider — first id still listed by the provider.
_PREFERRED: dict[str, tuple[str, ...]] = {
    "groq": (
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
    ),
    "openrouter": (
        "nvidia/nemotron-3-super-120b-a12b:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "google/gemini-2.0-flash-exp:free",
    ),
    "gemini": (
        "gemini-flash-latest",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ),
}

_audit_cache: tuple[float, list[dict[str, Any]] | None] = (0.0, None)


def _configured_model(provider: str) -> str:
    """Model id this deployment would use today (LLM_MODEL override aware)."""
    override = os.getenv("LLM_MODEL", "").strip()
    primary = os.getenv("LLM_PROVIDER", "").strip().lower()
    try:
        default = DEFAULT_MODELS[LLMProviderName(provider)]
    except ValueError:
        default = ""
    if override and primary == provider:
        return override
    return default


def _key_for(provider: str) -> str:
    env_names = {
        "groq": "GROQ_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "gemini": "GOOGLE_API_KEY",
    }
    return os.getenv(env_names.get(provider, ""), "").strip()


async def _fetch_model_ids(client: httpx.AsyncClient, provider: str) -> list[str]:
    """Return every model id the provider currently serves. Raises on failure."""
    key = _key_for(provider)
    headers = {"Accept": "application/json"}
    url = ""
    if provider in ("groq", "openrouter"):
        base = (
            "https://api.groq.com/openai/v1"
            if provider == "groq"
            else "https://openrouter.ai/api/v1"
        )
        url = f"{base}/models"
        if key:
            headers["Authorization"] = f"Bearer {key}"
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        return [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]

    # Gemini lists models with their supported generation methods.
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    headers["x-goog-api-key"] = key
    r = await client.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()
    ids: list[str] = []
    for m in data.get("models") or []:
        name = str(m.get("name") or "")
        if name.startswith("models/"):
            name = name[len("models/"):]
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" in methods:
            ids.append(name)
    return ids


def _pick_valid(provider: str, configured: str, served: list[str]) -> str | None:
    served_set = set(served)
    if configured and configured in served_set:
        return configured
    for candidate in _PREFERRED.get(provider, ()):
        if candidate in served_set:
            return candidate
    return served[0] if served else None


async def audit_providers(
    *, timeout: float = AUDIT_TIMEOUT_SECONDS, force: bool = False
) -> list[dict[str, Any]]:
    """Audit every keyed provider's live model catalogue.

    Rows: {provider, status(ok|fail|off), models_found, configured_model,
    valid, chosen_model, error}.
    """
    global _audit_cache
    now_mono = time.monotonic()
    if not force and _audit_cache[1] is not None and now_mono - _audit_cache[0] < AUDIT_CACHE_TTL:
        return _audit_cache[1] or []

    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for provider in ("groq", "openrouter", "gemini"):
            configured = _configured_model(provider)
            if not _key_for(provider):
                rows.append(
                    {
                        "provider": provider,
                        "status": "off",
                        "models_found": None,
                        "configured_model": configured,
                        "valid": False,
                        "chosen_model": None,
                        "error": "no API key configured",
                    }
                )
                continue
            try:
                served = await _fetch_model_ids(client, provider)
                chosen = _pick_valid(provider, configured, served)
                row = {
                    "provider": provider,
                    "status": "ok",
                    "models_found": len(served),
                    "configured_model": configured,
                    "valid": bool(configured and configured in set(served)),
                    "chosen_model": chosen,
                    "error": None,
                }
                if not row["valid"] and chosen:
                    logger.warning(
                        "llm_audit[%s]: configured model %r not served; chose %r",
                        provider,
                        configured,
                        chosen,
                    )
            except Exception as exc:  # noqa: BLE001 — audit never blocks serving
                row = {
                    "provider": provider,
                    "status": "fail",
                    "models_found": None,
                    "configured_model": configured,
                    "valid": False,
                    "chosen_model": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                logger.warning("llm_audit[%s] failed: %s", provider, exc)
            rows.append(row)

    _audit_cache = (time.monotonic(), rows)
    return rows


def invalidate_audit_cache() -> None:
    """Drop the cached audit (tests)."""
    global _audit_cache
    _audit_cache = (0.0, None)
