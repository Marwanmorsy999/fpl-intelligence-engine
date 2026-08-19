"""Phase 10.3 — Web dashboard tests."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fpl_intelligence.api.main import app

client = TestClient(app)


def test_dashboard_route_serves_html():
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert b"FPL Intelligence Dashboard" in resp.content


def test_dashboard_static_file_exists():
    base = Path(__file__).resolve().parent.parent.parent
    html_path = base / "src" / "fpl_intelligence" / "web" / "static" / "dashboard.html"
    assert html_path.exists()


def test_dashboard_html_contains_api_references():
    resp = client.get("/dashboard")
    body = resp.text
    assert "/api/v1/health" in body
    assert "/api/v1/intelligence/player/" in body
    assert "/api/v1/intelligence/unresolved" in body
