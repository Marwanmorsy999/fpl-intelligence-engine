"""Phase 10.3 — Simple web dashboard for FPL intelligence.

Serves a single-page dashboard that consumes the Phase 10.1 REST API to display
system health, player intelligence reports, and unresolved evidence.

Phase 11.2 (frontend separation): the dashboard routes are only registered when
``SERVE_STATIC_DASHBOARD`` is enabled, so the FastAPI app can run as a pure JSON
API while the static SPA is hosted separately (e.g. on Vercel/Netlify).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from fpl_intelligence.config import get_settings

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"


def _register_dashboard_routes() -> None:
    @router.get("/dashboard", include_in_schema=False)
    async def serve_dashboard() -> FileResponse:
        """Serve the single-page dashboard."""
        return FileResponse(_STATIC_DIR / "dashboard.html")

    @router.get("/api/v1/dashboard/squad-decisions", include_in_schema=False)
    async def dashboard_squad_decisions() -> JSONResponse:
        """Proxy the squad decisions for the dashboard SPA."""
        from fastapi.testclient import TestClient

        from fpl_intelligence.api.main import app

        client = TestClient(app)
        resp = client.get("/api/v1/decisions")
        if resp.status_code != 200:
            return JSONResponse(
                content={"error": "No squad configured. Use POST /api/v1/squad first."},
                status_code=resp.status_code,
            )
        return JSONResponse(content=resp.json())


if get_settings().serve_static_dashboard:
    _register_dashboard_routes()
