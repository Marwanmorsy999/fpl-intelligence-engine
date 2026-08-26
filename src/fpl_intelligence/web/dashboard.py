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

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from fpl_intelligence.config import get_settings

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"

#: Phase 19.0/20.0 page registry: route path -> static filename.
_PAGES: dict[str, str] = {
    "/dashboard": "dashboard.html",
    "/decisions": "dashboard.html",  # alias keeps the nav label honest
    "/my-team": "my_team.html",
    "/track-record": "track_record.html",
    "/live": "live.html",
    "/sources": "sources.html",
    "/connect": "connect.html",
    "/assistant": "assistant.html",  # Phase 20.0 weekly brief
    "/league": "league.html",  # Phase 23 Gate 1 — LEAGUE KILLER
    "/compare": "compare.html",  # Phase 24 Gate 0 — M3 head-to-head
    "/chips": "chips.html",  # Phase 24 Gate 1 — C1 chip planner
    "/crunch": "crunch.html",  # Phase 24 Gate 0 — M1 deadline crunch view
    "/targets": "targets.html",  # Phase 25 Gate 0 — T2 alpha engine
    "/planner": "planner.html",  # Phase 25 Gate 0 — T3 horizon planner
    "/transfers": "transfers.html",  # Phase 27 Gate 0 — T1 transfer desk
    "/help": "help.html",  # Phase 3.4 — onboarding & FAQ
}

#: Whitelisted shared assets servable under /static (no directory traversal).
#: Phase 3 adds lib/*: fetch-with-timeout, idb-cache, onboarding.
_STATIC_FILES = {
    "app.css",
    "tokens.css",
    "components.css",
    "app.js",
    "bookmarklet.js",
    "notify.js",
    "manifest.json",
    "sw.js",
    "offline.html",
    "icon-192.png",
    "icon-512.png",
}

#: Phase 3.1/3.3/3.4 — standalone browser libraries under /static/lib/.
#: Served via /static/lib/{name}; kept separate from the flat whitelist above
#: so legacy asset paths keep their exact shape (and contracts stay identical).
_LIB_FILES = {
    "fetch-with-timeout.js",
    "idb-cache.js",
    "onboarding.js",
}


def _sentry_browser_snippet(dsn: str) -> str:
    """Sentry browser snippet, injected into `dashboard.html` head only when a DSN exists.

    Keeps the frontend's "zero console noise" guarantee: when no DSN is
    configured the snippet is never served, so no SDK is loaded and every
    call-site guard (`window.reportError` / `window.Sentry?.`) is a no-op.
    """
    escaped = json.dumps(dsn)  # safe as a JS string literal
    return (
        '<script src="https://browser.sentry-cdn.com/8.41.1/bundle.tracing.es5.min.js" '
        'crossorigin="anonymous"></script>'
        "<script>\n"
        f"window.FPL_SENTRY_DSN = {escaped};\n"
        "try {\n"
        "  if (window.Sentry) { window.Sentry.init({ dsn: window.FPL_SENTRY_DSN, tracesSampleRate: 0.1 }); }\n"
        "} catch (e) { /* SDK init is enhancement-only */ }\n"
        "window.reportError = function (exc, context) {\n"
        "  try {\n"
        "    if (window.Sentry && window.Sentry.captureException) {\n"
        "      window.Sentry.setTag('context', String(context || '').slice(0, 80));\n"
        "      window.Sentry.captureException(exc);\n"
        "    }\n"
        "  } catch (e2) { /* reporting must never throw */ }\n"
        "};\n"
        "</script>"
    )


def _register_dashboard_routes() -> None:
    @router.get("/static/{asset_name}", include_in_schema=False)
    async def serve_static(asset_name: str) -> FileResponse:
        """Serve whitelisted shared assets only."""
        if asset_name not in _STATIC_FILES:
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(_STATIC_DIR / asset_name)

    @router.get("/static/lib/{lib_name}", include_in_schema=False)
    async def serve_static_lib(lib_name: str) -> FileResponse:
        """Phase 3 — serve whitelisted lib/ modules only."""
        if lib_name not in _LIB_FILES:
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(_STATIC_DIR / "lib" / lib_name)

    # Phase 4.4 — read once at registration time; absent DSN => never injected.
    _sentry_dsn_for_pages = os.environ.get("SENTRY_DSN", "").strip()

    def _page_handler(filename: str):
        # Only the primary dashboard page carries the Sentry browser snippet,
        # and only when a DSN is actually configured.
        if filename == "dashboard.html" and _sentry_dsn_for_pages:
            async def _serve() -> HTMLResponse:
                html = (_STATIC_DIR / filename).read_text(encoding="utf-8")
                snippet = _sentry_browser_snippet(_sentry_dsn_for_pages)
                return HTMLResponse(html.replace("</head>", snippet + "</head>", 1))

            return _serve

        async def _serve_file() -> FileResponse:
            return FileResponse(_STATIC_DIR / filename)

        return _serve_file

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
