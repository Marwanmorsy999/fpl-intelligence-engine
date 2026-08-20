"""Phase 11.3 — Player browser endpoint + squad schema example tests.

Exercises:

* ``GET /api/v1/players``            -> 200 + ingested players (id, web_name, team, position, price)
* ``GET /api/v1/players?team={id}``  -> 200 + players filtered to that team
* ``POST /api/v1/squad`` OpenAPI schema carries a valid example body with
  integer player IDs (no ``additionalProp`` placeholders).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.live_intelligence.bridge import StaticPredictionProvider
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider


@pytest.fixture
def client(populated_db: Session) -> TestClient:
    """Build a TestClient backed by the populated in-memory DB."""
    from fpl_intelligence.api.main import app

    def _override_db() -> Session:
        yield populated_db

    app.dependency_overrides[deps._get_db_session] = _override_db
    app.dependency_overrides[deps.get_llm_provider] = lambda: MockLLMProvider()
    app.dependency_overrides[deps.get_prediction_provider] = lambda: StaticPredictionProvider()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_list_players_returns_ingested_rows(client: TestClient) -> None:
    resp = client.get("/api/v1/players")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 4
    first = body[0]
    assert set(first.keys()) == {"id", "web_name", "team", "position", "price"}
    assert isinstance(first["id"], int)
    assert isinstance(first["web_name"], str)
    # Haaland (player 4) is a FWD (position 4) on team 4 with price 6.5.
    haaland = next(p for p in body if p["web_name"] == "Haaland")
    assert haaland["position"] == 4
    assert haaland["team"] == 4
    assert haaland["price"] == 6.5


def test_list_players_filters_by_team(client: TestClient) -> None:
    resp = client.get("/api/v1/players", params={"team": 4})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["web_name"] == "Haaland"


def _squad_schema(schema: dict) -> dict:
    """Resolve the SquadStateCreate schema (the request body is a ``$ref``)."""
    components = schema["components"]["schemas"]
    ref_path = (
        schema["paths"]["/api/v1/squad"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    )
    name = ref_path.split("/")[-1]
    return components[name]


def test_squad_openapi_example_uses_integer_ids(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    squad_schema = _squad_schema(schema)
    # The full-body example is surfaced via the ``example`` key.
    assert "example" in squad_schema
    example = squad_schema["example"]
    assert isinstance(example["player_ids"], list)
    assert all(isinstance(pid, int) for pid in example["player_ids"])
    assert len(example["player_ids"]) == 15
    # Dict fields show concrete integer keys, not ``additionalProp`` placeholders.
    for mapping_key in ("player_positions", "player_prices", "player_teams"):
        assert mapping_key in example
    # Per-field examples should also avoid additionalProp placeholders.
    # JSON object keys are always strings after serialization.
    props = squad_schema["properties"]
    assert props["player_positions"]["examples"][0] == {"1": 1, "2": 2, "3": 3, "4": 1, "11": 4}
    assert props["player_teams"]["examples"][0] == {"1": 3, "2": 3, "3": 1, "11": 8}


def test_squad_try_it_out_body_is_valid(client: TestClient) -> None:
    """The example body must pass the endpoint's own validation."""
    schema = client.get("/openapi.json").json()
    example = _squad_schema(schema)["example"]
    resp = client.post("/api/v1/squad", json=example)
    assert resp.status_code == 200
    stored = resp.json()
    assert stored["captain_id"] == example["captain_id"]
    assert len(stored["player_ids"]) == 15
