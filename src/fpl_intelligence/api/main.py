import logging
import os
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from fpl_intelligence import __version__
from fpl_intelligence.api.cache import EdgeCachePolicyMiddleware
from fpl_intelligence.api.deps import GetDB, assert_no_static_stub_in_production
from fpl_intelligence.api.performance import (
    RequestProfilingMiddleware,
    install_fpl_egress_timing,
    install_serialization_timing,
)
from fpl_intelligence.api.routes.admin import router as admin_router
from fpl_intelligence.api.routes.analyst import router as analyst_router
from fpl_intelligence.api.routes.assistant import router as assistant_router
from fpl_intelligence.api.routes.chips import router as chips_router
from fpl_intelligence.api.routes.compare import router as compare_router
from fpl_intelligence.api.routes.crests import router as crests_router
from fpl_intelligence.api.routes.data_sources import router as data_sources_router
from fpl_intelligence.api.routes.drawer import router as drawer_router
from fpl_intelligence.api.routes.fixtures import router as fixtures_router
from fpl_intelligence.api.routes.intelligence import router as intelligence_router
from fpl_intelligence.api.routes.league import router as league_router
from fpl_intelligence.api.routes.live import router as live_router
from fpl_intelligence.api.routes.news import router as news_router
from fpl_intelligence.api.routes.planner import router as planner_router
from fpl_intelligence.api.routes.players import router as players_router
from fpl_intelligence.api.routes.prices import router as prices_router
from fpl_intelligence.api.routes.push import router as push_router
from fpl_intelligence.api.routes.squad import router as squad_router
from fpl_intelligence.api.routes.sync import BookmarkletCorsMiddleware
from fpl_intelligence.api.routes.sync import router as sync_router
from fpl_intelligence.api.routes.targets import router as targets_router
from fpl_intelligence.api.routes.telegram import router as telegram_router
from fpl_intelligence.api.routes.transfers import router as transfers_router
from fpl_intelligence.common.logging import silence_credential_leaking_loggers
from fpl_intelligence.config import get_settings
from fpl_intelligence.web.dashboard import router as dashboard_router

settings = get_settings()
logger = logging.getLogger(__name__)

# Phase 4.4 — Sentry error tracking (free 5k errors/mo). Boot is guarded so the
# app runs perfectly when SENTRY_DSN is absent; the SDK only activates when a
# DSN is configured. traces_sample_rate=0.1 keeps volume well inside the free
# tier's monthly allowance.
_sentry_dsn = os.environ.get("SENTRY_DSN", "").strip()
if _sentry_dsn:
    import sentry_sdk  # noqa: PLC0415 - optional dependency, imported lazily

    sentry_sdk.init(dsn=_sentry_dsn, traces_sample_rate=0.1)

# Phase 15.0 — fail fast when a production deployment would serve the hardcoded
# StaticPredictionProvider stub (fake 5.5 xPTS for every player). This is the
# startup guard promised by the prediction-chain design; see deps.py.
assert_no_static_stub_in_production()


# Serverless platforms capture stdout/stderr verbatim, and httpx logs full
# request URLs (which embed the Telegram bot token) at INFO level. Mute those
# loggers before any client is built so credentials never reach the log stream.
silence_credential_leaking_loggers()

# Phase 0 — install internal hooks once at process startup. They only do work
# when a request-local PhaseTimer is active, so non-request/background code is
# unaffected. These hooks measure actual DB execution, FPL egress, optimizer
# prediction calls, and response-body serialization without changing results.
install_fpl_egress_timing()
install_serialization_timing()

app = FastAPI(title="FPL Intelligence Engine", version=__version__)
app.add_middleware(RequestProfilingMiddleware)


# Phase 19.1 — the bookmarklet POSTs to the sync push routes from
# fantasy.premierleague.com and needs CORS regardless of CORS_ORIGINS. Added
# before the generic middleware below, so a configured origins list stays the
# outermost (authoritative) layer; this one only fills the gaps.
app.add_middleware(BookmarkletCorsMiddleware)

# Phase 11.2 — allow a separately hosted frontend (Vercel/Netlify) to call this
# API. Origins are supplied as a comma-separated CORS_ORIGINS env var. An empty
# list means "no cross-origin access", which is the safe default.
_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Phase 4.3 — central edge-Cache-Control contract (Cloudflare honours these on
# the free plan; no paid rules required). Registered outermost so every readable
# API response picks up its policy. Session/personal endpoints are labelled
# "private, no-store" and are never cached at the edge.
app.add_middleware(EdgeCachePolicyMiddleware)

app.include_router(intelligence_router, prefix="/api/v1")
app.include_router(players_router, prefix="/api/v1")
app.include_router(squad_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(telegram_router, prefix="/api/v1")
app.include_router(dashboard_router)
app.include_router(analyst_router, prefix="/api/v1")
app.include_router(data_sources_router, prefix="/api/v1")
app.include_router(sync_router, prefix="/api/v1")
app.include_router(crests_router, prefix="/api/v1")
app.include_router(fixtures_router, prefix="/api/v1")
app.include_router(news_router, prefix="/api/v1")
# STEP 0 / Phase 4.3 — also serve the news endpoints at their documented bare
# paths (/news/bbc-rss, /news/radar) alongside the /api/v1-prefixed ones. The
# news router is intentionally mounted twice so both URLs validate.
app.include_router(news_router)
app.include_router(drawer_router, prefix="/api/v1")
app.include_router(assistant_router, prefix="/api/v1")
app.include_router(live_router, prefix="/api/v1")
app.include_router(league_router, prefix="/api/v1")
app.include_router(push_router, prefix="/api/v1")
app.include_router(prices_router, prefix="/api/v1")
app.include_router(compare_router, prefix="/api/v1")
app.include_router(chips_router, prefix="/api/v1")
# Phase 25 Gate 0 — transfer ledger + alpha engine + horizon planner.
app.include_router(transfers_router, prefix="/api/v1")
app.include_router(targets_router, prefix="/api/v1")
app.include_router(planner_router, prefix="/api/v1")


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/health")
async def health(db: GetDB) -> dict[str, str]:
    try:
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        db_status = "connected"
        status = "ok"
    except Exception:  # noqa: BLE001 - health must never crash
        logger.exception("health database probe failed")
        db_status = "error"
        status = "degraded"
    return {"status": status, "db": db_status, "version": __version__}


# --------------------------------------------------------------------------- #
# v2.7.5-decisions-heal — NEVER-500 safety net for the league + decisions
# surfaces.
#
# The v2.7.4 in-handler try/except only covers failures raised INSIDE the
# route body. A transient DB-connect failure inside the ``get_db`` dependency
# (or any other dependency) raises BEFORE the handler runs and used to escape
# as a raw 500 — exactly the /league outage observed at 03:20 UTC. This
# catch-all handler sits at ServerErrorMiddleware level, so it intercepts
# dependency-stage explosions too:
#
#   * /api/v1/league*      → 200 with an honest degraded payload (chips).
#   * /api/v1/decisions*   → 503 with a truthful detail (a skeleton report is
#                            never fabricated).
#   * everything else      → unchanged default behavior (plain 500 text).
# ---------------------------------------------------------------------------
from fastapi.responses import JSONResponse, PlainTextResponse  # noqa: E402


def _never_500_handler_factory() -> Any:

    async def handler(request: Any, exc: Exception) -> Any:
        path = request.url.path
        logger.exception("Unhandled exception for %s", path, exc_info=exc)
        # Phase 4.4 — report the unhandled exception to Sentry when configured.
        if _sentry_dsn:
            try:
                import sentry_sdk  # noqa: PLC0415,PLC2701

                sentry_sdk.capture_exception(exc)
            except Exception:  # noqa: BLE001 — Sentry must never break the API
                pass
        if path.startswith("/api/v1/league"):
            return JSONResponse(
                status_code=200,
                content={
                    "session_id": request.query_params.get("session_id", ""),
                    "status": "degraded",
                    "leagues": [],
                    "selected": None,
                    "needs_picker": False,
                    "note": (
                        "league data unavailable right now — render failed "
                        "server-side; retry or press Refresh"
                    ),
                    "honest_notes": [
                        "League page could not be computed right now — "
                        "showing a degraded state rather than failing."
                    ],
                },
            )
        if path.startswith("/api/v1/decisions"):
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "Decisions engine could not be computed right now "
                        "; retry shortly."
                    ),
                },
            )
        return PlainTextResponse("Internal Server Error", status_code=500)

    return handler


app.add_exception_handler(Exception, _never_500_handler_factory())
