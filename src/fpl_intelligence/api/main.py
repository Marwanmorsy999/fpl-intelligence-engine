from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from fpl_intelligence import __version__
from fpl_intelligence.api.routes.admin import router as admin_router
from fpl_intelligence.api.routes.intelligence import router as intelligence_router
from fpl_intelligence.api.routes.players import router as players_router
from fpl_intelligence.api.routes.squad import router as squad_router
from fpl_intelligence.api.routes.telegram import router as telegram_router
from fpl_intelligence.common.logging import silence_credential_leaking_loggers
from fpl_intelligence.config import get_settings
from fpl_intelligence.web.dashboard import router as dashboard_router

settings = get_settings()

# Serverless platforms capture stdout/stderr verbatim, and httpx logs full
# request URLs (which embed the Telegram bot token) at INFO level. Mute those
# loggers before any client is built so credentials never reach the log stream.
silence_credential_leaking_loggers()

app = FastAPI(title="FPL Intelligence Engine", version=__version__)

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


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
