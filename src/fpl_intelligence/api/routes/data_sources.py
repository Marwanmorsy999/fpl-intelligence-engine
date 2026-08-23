"""Phase 17.0 — Data Sources status endpoint.

Surfaces the live status of every external data source the engine depends on:
FPL import, Odds API, Understat, Weather, PL photos, and the LLM. This is the
answer to "where is the AI / where is the math / why is X off".
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter
from sqlalchemy import func

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


def _age_seconds_since(when: datetime) -> float:
    """Age of a stored timestamp in seconds (naive values treated as UTC)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - when).total_seconds())


#: Phase 20.1 — the two live reachability probes (FPL, PL photos CDN) used to
#: run sequentially with 8s timeouts each, which made the Sources page take
#: ~6s+ on Vercel where FPL is blocked. They now run concurrently with tight
#: budgets and the assembled payload is cached briefly in-process.
_PROBE_BUDGET_SECONDS = 3.0
_RESPONSE_CACHE_SECONDS = 60.0
_response_cache: tuple[float, dict[str, Any] | None] = (0.0, None)
_response_lock = threading.Lock()


async def _probe_fpl(settings: Any) -> tuple[str, str, str]:
    """FPL import reachability -> (status, detail, strategy)."""
    try:
        from fpl_intelligence.data_providers.fpl_egress import (  # noqa: PLC0415
            FplEgressChain,
            validate_entry_payload,
        )

        egress = FplEgressChain(
            settings.fpl_base_url,
            timeout=min(_PROBE_BUDGET_SECONDS, settings.egress_strategy_timeout),
            cache_ttl=0,  # never cache a health probe
        )
        await egress.fetch("/api/entry/1/", validator=validate_entry_payload)
        return "ok", "reachable", egress.winning_strategy or "direct"
    except Exception:  # noqa: BLE001
        pass
    try:
        async with httpx.AsyncClient(
            timeout=_PROBE_BUDGET_SECONDS, follow_redirects=True
        ) as client:
            r = await client.get(
                f"{settings.fpl_base_url.rstrip('/')}/api/entry/1/",
                headers={
                    "User-Agent": "FPL-Intelligence-Engine/1.0",
                    "Accept": "application/json",
                },
            )
        if r.status_code == 200:
            return "ok", "reachable", "direct"
        if r.status_code == 403:
            return "blocked", "rate-limited by FPL", ""
        return "degraded", f"HTTP {r.status_code}", ""
    except Exception as inner:  # noqa: BLE001
        return "blocked", f"unreachable ({type(inner).__name__})", ""


async def _probe_photos() -> tuple[str, str]:
    """Premier League CDN reachability -> (status, detail)."""
    try:
        async with httpx.AsyncClient(
            timeout=_PROBE_BUDGET_SECONDS, follow_redirects=True
        ) as client:
            r = await client.head("https://resources.premierleague.com/badges/70/t1.png")
        if r.status_code < 400:
            return "ok", "Premier League CDN"
        return "outage", f"HTTP {r.status_code}"
    except Exception:  # noqa: BLE001
        return "outage", "unreachable — avatars fallback active"


@router.get("/data-sources", summary="Live status of every data source")
async def data_sources(db: deps.GetDB) -> dict[str, Any]:
    """Return the live status of each external data source."""
    now_mono = time.monotonic()
    with _response_lock:
        cached = _response_cache
    if cached[1] is not None and now_mono - cached[0] < _RESPONSE_CACHE_SECONDS:
        return cached[1]

    settings = get_settings()

    # Live probes run concurrently so the worst case is one probe budget.
    fpl_task = asyncio.create_task(_probe_fpl(settings))
    photos_task = asyncio.create_task(_probe_photos())
    fpl_status, fpl_detail, fpl_strategy = await fpl_task
    photos_status, photos_detail = await photos_task

    now = datetime.now(UTC).isoformat()

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

    # --- Phase 20.1: fixtures scan + BBC news radar states (materialized) -----
    from sqlalchemy import select  # noqa: PLC0415

    from fpl_intelligence.materialize.service import (  # noqa: PLC0415
        FIXTURES_MAX_AGE_SECONDS,
        NEWS_MAX_AGE_SECONDS,
    )
    from fpl_intelligence.sync.materialized_models import (  # noqa: PLC0415
        FixturesCacheDB,
        NewsCacheDB,
        PredictionCurrentDB,
    )
    from fpl_intelligence.sync.models import IngestedGameweekDB

    fx_row = db.scalar(
        select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()).limit(1)
    )
    if fx_row is not None:
        age_h = _age_seconds_since(fx_row.fetched_at) / 3600
        n_fix = len(fx_row.payload or [])
        if age_h * 3600 <= FIXTURES_MAX_AGE_SECONDS and n_fix:
            fixtures_status = "ok"
            fixtures_detail = (
                f"{n_fix} fixtures cached {age_h:.1f}h ago (source: {fx_row.source})"
            )
        else:
            fixtures_status = "stale"
            fixtures_detail = f"cache is {age_h:.0f}h old — waiting for cron"
    else:
        fixtures_status = "pending"
        fixtures_detail = "no cached fixtures yet — run /api/v1/admin/materialize"

    news_row = db.scalar(select(NewsCacheDB).order_by(NewsCacheDB.id.desc()).limit(1))
    if news_row is not None:
        age_min = _age_seconds_since(news_row.fetched_at) / 60
        if age_min * 60 <= NEWS_MAX_AGE_SECONDS and news_row.headline_count:
            bbc_status = "ok"
            bbc_detail = (
                f"{news_row.headline_count} headlines cached "
                f"{int(age_min)} min ago ({news_row.source})"
            )
        else:
            bbc_status = "stale"
            bbc_detail = f"last scan {int(age_min / 60)}h ago"
    else:
        bbc_status = "pending"
        bbc_detail = "no news scan yet — runs on squad pages"

    last_ingest = db.scalar(
        select(IngestedGameweekDB.ingested_at)
        .order_by(IngestedGameweekDB.ingested_at.desc())
        .limit(1)
    )
    ingested_gws = sorted(
        {
            int(gw)
            for (gw,) in db.execute(select(IngestedGameweekDB.gameweek).distinct()).all()
        }
    )
    if last_ingest is not None:
        ingest_age_h = _age_seconds_since(last_ingest) / 3600
        vaastav_status = "ok" if ingested_gws else "empty"
        vaastav_detail = (
            f"GWs {ingested_gws[-3:]} ingested, last {ingest_age_h:.1f}h ago"
            if ingested_gws
            else f"ingest ran but no GW rows yet ({ingest_age_h:.1f}h ago)"
        )
    else:
        vaastav_status = "pending"
        vaastav_detail = "no vaastav results ingested yet — daily 06:10 UTC cron"

    pred_rows = db.execute(
        select(PredictionCurrentDB.gameweek, func.count())
        .group_by(PredictionCurrentDB.gameweek)
        .order_by(PredictionCurrentDB.gameweek)
    ).all()
    pred_last = db.scalar(
        select(PredictionCurrentDB.computed_at)
        .order_by(PredictionCurrentDB.computed_at.desc())
        .limit(1)
    )
    if pred_rows:
        pred_age_h = (
            _age_seconds_since(pred_last) / 3600 if pred_last is not None else None
        )
        gw_txt = ",".join(f"GW{gw}({n})" for gw, n in pred_rows[:6])
        predictions_status = "ok" if (pred_age_h or 0) <= 36 else "stale"
        predictions_detail = f"xPTS for {gw_txt} — computed {pred_age_h:.1f}h ago"
    else:
        predictions_status = "pending"
        predictions_detail = "no precomputed xPTS yet — daily 06:10 UTC cron"

    payload = {
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
            "vaastav_results": {
                "status": vaastav_status,
                "detail": vaastav_detail,
            },
            "predictions_materialized": {
                "status": predictions_status,
                "detail": predictions_detail,
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
    with _response_lock:
        globals()["_response_cache"] = (time.monotonic(), payload)
    return payload
