"""Phase 17.0 — Data Sources status endpoint.

Surfaces the live status of every external data source the engine depends on:
FPL import, Odds API, Understat, Weather, PL photos, and the LLM. This is the
answer to "where is the AI / where is the math / why is X off".
"""
from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from fpl_intelligence.api import deps
from fpl_intelligence.config import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)


def _file_age_days(path: str) -> float | None:
    try:
        age = time.time() - os.path.getmtime(path)
        return round(age / 86400.0, 1)
    except OSError:
        return None


@router.get("/data-sources", summary="Live status of every data source")
async def data_sources(db: deps.GetDB) -> dict[str, Any]:
    """Return the live status of each external data source."""
    settings = get_settings()
    now = datetime.now(UTC).isoformat()

    # --- FPL import: test reachability of the entry endpoint -----------------
    fpl_status = "unknown"
    fpl_detail = ""
    try:
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            r = await client.get(
                f"{settings.fpl_base_url.rstrip('/')}/api/entry/1/",
                headers={"User-Agent": "FPL-Intelligence-Engine/1.0", "Accept": "application/json"},
            )
            if r.status_code == 200:
                fpl_status = "ok"
                fpl_detail = "reachable"
            elif r.status_code == 403:
                fpl_status = "blocked"
                fpl_detail = "rate-limited by FPL"
            else:
                fpl_status = "degraded"
                fpl_detail = f"HTTP {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        fpl_status = "blocked"
        fpl_detail = f"unreachable ({type(exc).__name__})"

    # --- Odds API: enabled only when key is present ---------------------------
    odds_key_present = bool(os.getenv("THE_ODDS_API_KEY", "").strip())
    odds_status = "enabled" if odds_key_present else "off"
    odds_detail = "key configured" if odds_key_present else "THE_ODDS_API_KEY not set"

    # --- Understat: snapshot age ---------------------------------------------
    understat_path = "data/seed/understat_snapshot.json"
    understat_age = _file_age_days(understat_path)
    if understat_age is None:
        understat_status = "off"
        understat_detail = "no snapshot found"
    elif understat_age > 14:
        understat_status = "stale"
        understat_detail = f"snapshot {understat_age}d old"
    else:
        understat_status = "ok"
        understat_detail = f"snapshot {understat_age}d old"

    # --- Weather: always live (Open-Meteo, no key) ----------------------------
    weather_status = "live"
    weather_detail = "Open-Meteo (no key required)"

    # --- PL photos: CDN reachable --------------------------------------------
    photos_status = "ok"
    photos_detail = "Premier League CDN"
    try:
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            r = await client.head("https://resources.premierleague.com/badges/70/t1.png")
            if r.status_code >= 400:
                photos_status = "outage"
                photos_detail = f"HTTP {r.status_code}"
    except Exception:  # noqa: BLE001
        photos_status = "outage"
        photos_detail = "unreachable — avatars fallback active"

    # --- LLM: which provider/model is configured -----------------------------
    llm_provider = os.getenv("LLM_PROVIDER", "mock").strip() or "mock"
    groq_present = bool(os.getenv("GROQ_API_KEY", "").strip())
    openrouter_present = bool(os.getenv("OPENROUTER_API_KEY", "").strip())
    gemini_present = bool(os.getenv("GOOGLE_API_KEY", "").strip())
    real_keys = []
    if groq_present:
        real_keys.append("GROQ")
    if openrouter_present:
        real_keys.append("OPENROUTER")
    if gemini_present:
        real_keys.append("GEMINI")

    if llm_provider == "mock":
        llm_status = "template-fallback"
        keys_note = f" (keys available: {', '.join(real_keys)})" if real_keys else ""
        llm_detail = f"mock provider{keys_note}"
    else:
        llm_status = "enabled"
        llm_detail = f"{llm_provider}" + (f" + {', '.join(real_keys)} keys" if real_keys else "")

    return {
        "as_of": now,
        "sources": {
            "fpl_import": {
                "status": fpl_status,
                "detail": fpl_detail,
                "retry_schedule": "daily 06:30 UTC",
            },
            "odds_api": {
                "status": odds_status,
                "detail": odds_detail,
            },
            "understat": {
                "status": understat_status,
                "detail": understat_detail,
            },
            "weather": {
                "status": weather_status,
                "detail": weather_detail,
            },
            "pl_photos": {
                "status": photos_status,
                "detail": photos_detail,
            },
            "llm": {
                "status": llm_status,
                "detail": llm_detail,
            },
        },
    }
