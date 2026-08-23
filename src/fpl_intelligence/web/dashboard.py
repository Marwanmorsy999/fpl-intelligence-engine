"""Phase 10.3 — Simple web dashboard for FPL intelligence.

Serves the single-page decisions dashboard that consumes the Phase 10.1 REST
API to display system health, player intelligence reports, and unresolved
evidence.

Phase 11.2 (frontend separation): the dashboard routes are only registered when
``SERVE_STATIC_DASHBOARD`` is enabled, so the FastAPI app can run as a pure JSON
API while the static SPA is hosted separately (e.g. on Vercel/Netlify).

Phase 19.0 (multi-page UI): adds the FotMob-grade sibling pages — My Team,
Track Record, Live, Sources and Connect — plus a whitelisted ``/static``
handler for the shared stylesheet/scripts.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from fpl_intelligence.config import get_settings

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"

#: Phase 19.0 page registry: route path -> static filename.
_PAGES: dict[str, str] = {
    "/dashboard": "dashboard.html",
    "/decisions": "dashboard.html",  # alias keeps the nav label honest
    "/my-team": "my_team.html",
    "/track-record": "track_record.html",
    "/live": "live.html",
    "/sources": "sources.html",
    "/connect": "connect.html",
}

#: Whitelisted shared assets servable under /static (no directory traversal).
_STATIC_FILES = {"app.css", "app.js", "bookmarklet.js"}


def _register_dashboard_routes() -> None:
    @router.get("/static/{asset_name}", include_in_schema=False)
    async def serve_static(asset_name: str) -> FileResponse:
        """Serve whitelisted shared assets only."""
        if asset_name not in _STATIC_FILES:
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(_STATIC_DIR / asset_name)

    def _page_handler(filename: str):
        async def _serve() -> FileResponse:
            return FileResponse(_STATIC_DIR / filename)

        return _serve

    for path, filename in _PAGES.items():
        router.get(path, include_in_schema=False)(_page_handler(filename))

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
