"""Phase 10.3 — Simple web dashboard for FPL intelligence.

Serves the bundled static dashboard and its sibling pages from the same FastAPI
application as the API. The Vercel deployment is intentionally self-contained:
there is no separate frontend host to configure, so the dashboard routes must
always be registered in this application.
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

_PAGES: dict[str, str] = {
    "/dashboard": "dashboard.html",
    "/decisions": "dashboard.html",
    "/my-team": "my_team.html",
    "/track-record": "track_record.html",
    "/live": "live.html",
    "/sources": "sources.html",
    "/connect": "connect.html",
    "/assistant": "assistant.html",
    "/league": "league.html",
    "/compare": "compare.html",
    "/chips": "chips.html",
    "/crunch": "crunch.html",
    "/targets": "targets.html",
    "/planner": "planner.html",
    "/transfers": "transfers.html",
    "/help": "help.html",
}

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

_LIB_FILES = {
    "fetch-with-timeout.js",
    "idb-cache.js",
    "onboarding.js",
}


def _sentry_browser_snippet(dsn: str) -> str:
    """Sentry browser snippet, injected only when a DSN is configured."""
    escaped = json.dumps(dsn)
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
        if asset_name not in _STATIC_FILES:
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(_STATIC_DIR / asset_name)

    @router.get("/static/lib/{lib_name}", include_in_schema=False)
    async def serve_static_lib(lib_name: str) -> FileResponse:
        if lib_name not in _LIB_FILES:
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(_STATIC_DIR / "lib" / lib_name)

    _sentry_dsn_for_pages = os.environ.get("SENTRY_DSN", "").strip()

    def _page_handler(filename: str):
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
    async def dashboard_squad_decisions(session_id: str | None = None) -> JSONResponse:
        from fpl_intelligence.api import deps
        from fpl_intelligence.api.routes.squad import build_decisions_payload

        db_gen = deps._get_db_session()
        db = next(db_gen)
        try:
            key = session_id or _last_saved_session_id(db)
            if not key:
                return JSONResponse(
                    content={"error": "No squad configured. Use POST /api/v1/squad first."},
                    status_code=404,
                )
            provider = deps.get_prediction_provider(db)  # type: ignore[arg-type]
            report = await build_decisions_payload(db, provider, key)
            return JSONResponse(content=report.model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                content={"error": f"Decisions unavailable right now ({type(exc).__name__})."},
                status_code=503,
            )
        finally:
            try:
                next(db_gen, None)
            except StopIteration:
                pass

    def _last_saved_session_id(db: object) -> str | None:
        from sqlalchemy import select as _select
        from fpl_intelligence.squad.models_db import SquadStateDB

        try:
            row = db.execute(  # type: ignore[union-attr]
                _select(SquadStateDB.session_id).order_by(SquadStateDB.updated_at.desc())
            ).scalars().first()
            return str(row) if row else None
        except Exception:
            return None


# This deployment is designed to serve the dashboard and API from one FastAPI
# function. Register the routes unconditionally so a stale SERVE_STATIC_DASHBOARD
# environment variable cannot silently remove the UI from production.
_register_dashboard_routes()
