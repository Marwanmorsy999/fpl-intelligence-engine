"""Phase 10.3 / 13.0 - Web dashboard tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fpl_intelligence.api.main import app

client = TestClient(app)


def test_dashboard_route_serves_html():
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert b"FPL Intelligence" in resp.content
    # Phase 13.0 one-click UX.
    assert b"Analyze My Team" in resp.content
    assert b"Enter your FPL Team ID" in resp.content


def test_dashboard_static_file_exists():
    base = Path(__file__).resolve().parent.parent.parent
    html_path = base / "src" / "fpl_intelligence" / "web" / "static" / "dashboard.html"
    assert html_path.exists()


def test_dashboard_html_contains_api_references():
    resp = client.get("/dashboard")
    body = resp.text
    # Phase 13.0 one-click flow endpoints.
    assert "/api/v1/squad/from-fpl" in body
    assert "/api/v1/decisions" in body
