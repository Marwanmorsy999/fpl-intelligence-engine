"""Phase 10.3 — Simple web dashboard for FPL intelligence.

Serves a single-page dashboard that consumes the Phase 10.1 REST API to display
system health, player intelligence reports, and unresolved evidence.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"


@router.get("/dashboard", include_in_schema=False)
async def serve_dashboard() -> FileResponse:
    """Serve the single-page dashboard."""
    return FileResponse(_STATIC_DIR / "dashboard.html")
