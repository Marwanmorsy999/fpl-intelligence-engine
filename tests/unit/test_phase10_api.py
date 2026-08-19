"""Phase 10.1 — API endpoint tests.

All four endpoints are exercised with the LLM and DB seams mocked/in-memory so
the suite runs fully offline and instantly:

* ``GET  /api/v1/health``            -> 200 + monitoring snapshot
* ``GET  /api/v1/intelligence/player/{id}`` -> 200 + IntelligenceReport (JSON + MD)
* ``POST /api/v1/ingest``            -> 200 + ingestion summary + extraction ids
* ``GET  /api/v1/intelligence/unresolved`` -> 200 + paginated unresolved rows
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# Import the Phase 9 models so their tables register on Base.metadata before the
# in-memory ``db_session`` fixture calls ``create_all`` (the fixture resolves its
# dependencies before this module's ``client`` fixture body runs).
from fpl_intelligence import live_intelligence  # noqa: F401
from fpl_intelligence.api import deps
from fpl_intelligence.live_intelligence.bridge import StaticPredictionProvider
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider


@pytest.fixture
def client(db_session: Session) -> TestClient:
    """Build a TestClient with every external seam replaced by test doubles."""
    from fpl_intelligence.api.main import app

    def _override_db() -> Session:
        yield db_session

    app.dependency_overrides[deps._get_db_session] = _override_db
    app.dependency_overrides[deps.get_llm_provider] = lambda: MockLLMProvider()
    app.dependency_overrides[deps.get_prediction_provider] = lambda: StaticPredictionProvider()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_health_endpoint(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert body["deployment_tag"] == "v0.9.8-production-deployment"
    assert body["phase9_8_deployment"]["status"] == "closed"
    assert "monitoring" in body
    assert "health" in body["monitoring"]
    assert "metrics" in body["monitoring"]


def test_player_report_json(client: TestClient) -> None:
    resp = client.get("/api/v1/intelligence/player/4", params={"gameweek": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"] == "transfer_recommendation"
    assert body["prediction_context"]["player_id"] == 4
    assert body["prediction_context"]["gameweek"] == 2
    assert "is_mock" in body


def test_player_report_markdown_via_query(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/intelligence/player/4",
        params={"gameweek": 2, "format": "md"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "#" in resp.text


def test_player_report_markdown_via_accept_header(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/intelligence/player/4",
        params={"gameweek": 2},
        headers={"Accept": "text/markdown"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")


def test_ingest_endpoint(client: TestClient) -> None:
    payload = {
        "source_id": "press_conference_test",
        "content_text": "The manager confirmed a 4-4-2 formation for the next match.",
        "published_at": "2025-08-10T10:00:00+00:00",
        "url": "https://example.com/report",
    }
    resp = client.post("/api/v1/ingest", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("created", "duplicate", "rejected")
    assert "content_hash" in body
    # The deterministic mock extractor fires on 'formation' -> tactical evidence.
    assert isinstance(body["tactical_evidence_ids"], list)
    assert isinstance(body["unresolved_evidence_ids"], list)


def test_ingest_endpoint_rejects_bad_timestamp(client: TestClient) -> None:
    payload = {
        "source_id": "press_conference_test",
        "content_text": "Some news.",
        "published_at": "not-a-date",
    }
    resp = client.post("/api/v1/ingest", json=payload)
    assert resp.status_code == 422


def test_unresolved_endpoint(client: TestClient) -> None:
    resp = client.get("/api/v1/intelligence/unresolved", params={"limit": 10, "offset": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int)
    assert body["limit"] == 10
    assert body["offset"] == 0
