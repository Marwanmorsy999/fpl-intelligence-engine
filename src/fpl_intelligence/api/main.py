from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from fpl_intelligence import __version__
from fpl_intelligence.api.deps import GetDB, assert_no_static_stub_in_production
from fpl_intelligence.api.routes.admin import router as admin_router
from fpl_intelligence.api.routes.analyst import router as analyst_router
from fpl_intelligence.api.routes.assistant import router as assistant_router
from fpl_intelligence.api.routes.crests import router as crests_router
from fpl_intelligence.api.routes.data_sources import router as data_sources_router
from fpl_intelligence.api.routes.drawer import router as drawer_router
from fpl_intelligence.api.routes.fixtures import router as fixtures_router
from fpl_intelligence.api.routes.intelligence import router as intelligence_router
from fpl_intelligence.api.routes.league import router as league_router
from fpl_intelligence.api.routes.live import router as live_router
from fpl_intelligence.api.routes.news import router as news_router
from fpl_intelligence.api.routes.players import router as players_router
from fpl_intelligence.api.routes.prices import router as prices_router
from fpl_intelligence.api.routes.push import router as push_router
from fpl_intelligence.api.routes.squad import router as squad_router
from fpl_intelligence.api.routes.sync import BookmarkletCorsMiddleware
from fpl_intelligence.api.routes.sync import router as sync_router
from fpl_intelligence.api.routes.telegram import router as telegram_router
from fpl_intelligence.common.logging import silence_credential_leaking_loggers
from fpl_intelligence.config import get_settings
from fpl_intelligence.web.dashboard import router as dashboard_router

settings = get_settings()

# Phase 15.0 — fail fast when a production deployment would serve the hardcoded
# StaticPredictionProvider stub (fake 5.5 xPTS for every player). This is the
# startup guard promised by the prediction-chain design; see deps.py.
assert_no_static_stub_in_production()


# Serverless platforms capture stdout/stderr verbatim, and httpx logs full
# request URLs (which embed the Telegram bot token) at INFO level. Mute those
# loggers before any client is built so credentials never reach the log stream.
silence_credential_leaking_loggers()

app = FastAPI(title="FPL Intelligence Engine", version=__version__)

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
app.include_router(drawer_router, prefix="/api/v1")
app.include_router(assistant_router, prefix="/api/v1")
app.include_router(live_router, prefix="/api/v1")
app.include_router(league_router, prefix="/api/v1")
app.include_router(push_router, prefix="/api/v1")
app.include_router(prices_router, prefix="/api/v1")
# TEMP debug for v2.3.2 breakdown — remove after verification
try:
    from fpl_intelligence.api.routes.debug_breakdown import router as debug_router
    app.include_router(debug_router, prefix="/api/v1")
except Exception:
    pass


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/health")
async def health(db: GetDB) -> dict[str, str]:
    try:
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        db_status = "connected"
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - health must never crash
        db_status = f"error: {exc}"
        status = "degraded"
    return {"status": status, "db": db_status, "version": __version__}
