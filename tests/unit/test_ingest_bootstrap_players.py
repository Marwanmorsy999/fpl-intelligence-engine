"""Task 3 regression — ``ingest_bootstrap`` must populate team + price.

The Browse Players endpoint (``GET /api/v1/players``) derives ``team`` from
``PlayerTeamMembership`` and ``price`` from ``PlayerGameweekPerformance``.
The bootstrap ingest path previously created ``Player`` and ``Team`` rows but
never linked the player to a team (it ignored the element's ``team`` field)
and never stored a price snapshot, so every row came back with
``team=null`` and ``price=null``.

This test proves the fix: after ``ingest_bootstrap`` with a mocked provider,
``/api/v1/players`` returns the linked team id and the ``now_cost``-derived
price (FPL ``now_cost`` is in tenths of £m, so 65 -> 6.5).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.ingestion.fpl import ingest_bootstrap
from fpl_intelligence.live_intelligence.bridge import StaticPredictionProvider
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider


class _FakeBootstrapProvider:
    """Minimal stand-in for OfficialFPLDataProvider.get_bootstrap_static."""

    def get_bootstrap_static(self) -> dict:
        return {
            "teams": [
                {"id": 1, "name": "Arsenal", "short_name": "ARS"},
                {"id": 2, "name": "Manchester City", "short_name": "MCI"},
            ],
            "elements": [
                {
                    "id": 1,
                    "first_name": "Erling",
                    "second_name": "Haaland",
                    "web_name": "Haaland",
                    "element_type": 4,
                    "team": 2,
                    "now_cost": 65,
                },
                {
                    "id": 2,
                    "first_name": "Bukayo",
                    "second_name": "Saka",
                    "web_name": "Saka",
                    "element_type": 3,
                    "team": 1,
                    "now_cost": 100,
                },
            ],
            "events": [{"id": 1, "name": "Gameweek 1"}],
        }


def test_ingest_bootstrap_populates_team_and_price(db_session: Session) -> None:
    ingest_bootstrap(db_session, _FakeBootstrapProvider(), "2026-27")

    from fpl_intelligence.api.main import app

    def _override_db() -> Session:
        yield db_session

    app.dependency_overrides[deps._get_db_session] = _override_db
    app.dependency_overrides[deps.get_llm_provider] = lambda: MockLLMProvider()
    app.dependency_overrides[deps.get_prediction_provider] = lambda: StaticPredictionProvider()
    try:
        with TestClient(app) as client:
            resp = client.get("/api/v1/players")
            assert resp.status_code == 200
            body = resp.json()
            assert len(body) == 2

            haaland = next(p for p in body if p["web_name"] == "Haaland")
            saka = next(p for p in body if p["web_name"] == "Saka")

            # Haaland -> Manchester City (team id 2), FWD (position 4), £6.5m.
            assert haaland["team"] == 2
            assert haaland["position"] == 4
            assert haaland["price"] == 6.5

            # Saka -> Arsenal (team id 1), MID (position 3), £10.0m.
            assert saka["team"] == 1
            assert saka["position"] == 3
            assert saka["price"] == 10.0
    finally:
        app.dependency_overrides.clear()
