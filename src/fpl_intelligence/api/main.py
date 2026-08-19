from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fpl_intelligence import __version__
from fpl_intelligence.api.routes.intelligence import router as intelligence_router
from fpl_intelligence.api.routes.squad import router as squad_router
from fpl_intelligence.config import get_settings
from fpl_intelligence.web.dashboard import router as dashboard_router

settings = get_settings()

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
app.include_router(squad_router, prefix="/api/v1")
app.include_router(dashboard_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
