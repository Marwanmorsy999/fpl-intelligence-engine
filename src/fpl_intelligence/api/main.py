from fastapi import FastAPI

from fpl_intelligence import __version__
from fpl_intelligence.api.routes.intelligence import router as intelligence_router

app = FastAPI(title="FPL Intelligence Engine", version=__version__)

app.include_router(intelligence_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
