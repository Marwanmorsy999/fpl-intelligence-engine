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
    fpl_strategy = ""
    try:
        import httpx  # noqa: PLC0415

        # Use the egress chain so the status reflects the path the importer
        # actually uses — including which mask won (Phase 18.0).
        from fpl_intelligence.data_providers.fpl_egress import (  # noqa: PLC0415
            FplEgressChain,
            validate_entry_payload,
        )

        egress = FplEgressChain(
            settings.fpl_base_url,
            timeout=min(8.0, settings.egress_strategy_timeout),
            cache_ttl=0,  # never cache a health probe
        )
        await egress.fetch("/api/entry/1/", validator=validate_entry_payload)
        fpl_status = "ok"
        fpl_detail = "reachable"
        fpl_strategy = egress.winning_strategy or "direct"
    except Exception:  # noqa: BLE001
        # Fall back to a plain direct probe if the chain probe fails.
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                r = await client.get(
                    f"{settings.fpl_base_url.rstrip('/')}/api/entry/1/",
                    headers={
                        "User-Agent": "FPL-Intelligence-Engine/1.0",
                        "Accept": "application/json",
                    },
                )
                if r.status_code == 200:
                    fpl_status = "ok"
                    fpl_detail = "reachable"
                    fpl_strategy = "direct"
                elif r.status_code == 403:
                    fpl_status = "blocked"
                    fpl_detail = "rate-limited by FPL"
                else:
                    fpl_status = "degraded"
                    fpl_detail = f"HTTP {r.status_code}"
        except Exception as inner:  # noqa: BLE001
            fpl_status = "blocked"
            fpl_detail = f"unreachable ({type(inner).__name__})"

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

    # --- LLM: which provider/model the analyst will actually use -------------
    # Mirrors analyst._build_real_provider(): any real key engages the live
    # router (GROQ -> OPENROUTER -> GEMINI), independent of LLM_PROVIDER.
    groq_present = bool(os.getenv("GROQ_API_KEY", "").strip())
    openrouter_present = bool(os.getenv("OPENROUTER_API_KEY", "").strip())
    gemini_present = bool(os.getenv("GOOGLE_API_KEY", "").strip())
    configured_order = []
    if groq_present:
        configured_order.append("groq")
    if openrouter_present:
        configured_order.append("openrouter")
    if gemini_present:
        configured_order.append("gemini")

    if configured_order:
        llm_status = "enabled"
        llm_detail = "live LLM (" + " -> ".join(configured_order) + ")"
    else:
        llm_status = "template-fallback"
        llm_detail = "no LLM keys configured"

    # --- Phase 20.0: fixtures scan + BBC news radar cache states -------------
    from fpl_intelligence.api.routes.fixtures import (  # noqa: PLC0415
        _fixtures_cache as fx_cache,
    )
    from fpl_intelligence.api.routes.news import (  # noqa: PLC0415
        NEWS_TTL_SECONDS,
    )
    from fpl_intelligence.api.routes.news import (
        _feed_cache as news_cache,
    )

    if fx_cache is not None and time.time() - fx_cache[0] < settings.egress_cache_ttl:
        fixtures_status = "ok"
        fixtures_detail = (
            f"{len(fx_cache[1])} fixtures · scanned {int(time.time() - fx_cache[0])}s ago"
        )
    else:
        fixtures_status = "pending"
        fixtures_detail = "first scan runs on demand from My Team / Decisions"

    if news_cache is not None and time.time() - news_cache[0] < NEWS_TTL_SECONDS:
        age_min = int((time.time() - news_cache[0]) / 60)
        bbc_status = "ok"
        bbc_detail = f"{len(news_cache[1])} headlines cached {age_min} min ago"
    else:
        bbc_status = "pending"
        bbc_detail = "no fresh scan yet — runs on squad pages"

    return {
        "as_of": now,
        "sources": {
            "fixtures": {
                "status": fixtures_status,
                "detail": fixtures_detail,
            },
            "bbc_news": {
                "status": bbc_status,
                "detail": bbc_detail,
            },
            "fpl_import": {
                "status": fpl_status,
                "detail": fpl_detail + (f" · via {fpl_strategy}" if fpl_strategy else ""),
                "egress_strategy": fpl_strategy or "unprobed",
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
