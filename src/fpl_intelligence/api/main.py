from fastapi import FastAPI

from fpl_intelligence import __version__

app = FastAPI(title="FPL Intelligence Engine", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
