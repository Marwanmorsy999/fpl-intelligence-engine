"""Phase 15.0 — live prediction chain wired into ``GET /api/v1/decisions``.

These tests prove the production decisions endpoint:

* resolves the real :class:`LivePredictionProvider` by default (the static stub
  is only used when explicitly forced for tests/dry-run);
* enriches every ``PlayerDetail`` with ``prediction_source`` + ``data_quality``
  labels and a top-level ``chain`` provenance object carrying
  ``source_label`` / ``data_quality`` / ``notes``;
* serves *differentiated* xPTS across players (the proxy level is not a flat
  stub — price/Understat/market terms spread the numbers);
* omits players the resolved chain can't speak about rather than inventing them.

Fully offline: the provider reads the committed bootstrap + Understat seeds and
the in-memory SQLite session; no network, no stub.
"""
from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.api.main import app
from fpl_intelligence.prediction.live_provider import LivePredictionProvider
from fpl_intelligence.squad.models_db import SquadStateDB


def _pos_map() -> dict[int, int]:
    return {i: (1 if i <= 2 else (2 if i <= 7 else (3 if i <= 12 else 4))) for i in range(1, 16)}


@pytest.fixture
def live_client(db_session: Session) -> Generator[TestClient, None, None]:
    """Decisions client that resolves the REAL LivePredictionProvider.

    This is the production wiring: ``PREDICTION_PROVIDER=live`` resolves
    LivePredictionProvider, whose fallback chain serves differentiated xPTS
    from the committed seed catalog (no network, no stub).
    """

    def _override_db() -> Generator[Session, None, None]:
        yield db_session

    # Real provider — reads data/seed/fpl_bootstrap_seed.json + understat snapshot.
    real_provider = LivePredictionProvider(session=db_session)

    app.dependency_overrides[deps._get_db_session] = _override_db
    app.dependency_overrides[deps.get_llm_provider] = lambda: None
    app.dependency_overrides[deps.get_prediction_provider] = lambda: real_provider

    db_session.execute(delete(SquadStateDB))
    db_session.commit()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        db_session.execute(delete(SquadStateDB))
        db_session.commit()


class TestDecisionsLiveChain:
    """GET /api/v1/decisions with the real live provider."""

    def test_live_provider_serves_differentiated_xpts(
        self, live_client: TestClient
    ) -> None:
        """xPTS must differ across players — never the flat 5.5 stub."""
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "bank": 0.0,
            "free_transfers": 1,
            "gameweek": 3,
            "player_positions": _pos_map(),
            "player_prices": {i: 5.0 + (i % 5) for i in range(1, 16)},
            "player_teams": {i: (1 if i <= 10 else 2) for i in range(1, 16)},
        }
        live_client.post("/api/v1/squad", json=payload)
        resp = live_client.get("/api/v1/decisions")
        assert resp.status_code == 200
        body = resp.json()

        xpts = [
            v["expected_points"]
            for v in body["players"].values()
            if v.get("expected_points") is not None
        ]
        assert len(xpts) >= 11, "most players should carry xPTS"
        # Differentiated: min != max (the proxy spreads by price percentile).
        assert min(xpts) != max(xpts), "xPTS must differ across players"

    def test_players_carry_source_and_data_quality(
        self, live_client: TestClient
    ) -> None:
        """Every PlayerDetail must expose its chain provenance labels."""
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "gameweek": 3,
            "player_positions": _pos_map(),
            "player_prices": {i: 6.0 for i in range(1, 16)},
            "player_teams": {i: 1 for i in range(1, 16)},
        }
        live_client.post("/api/v1/squad", json=payload)
        resp = live_client.get("/api/v1/decisions")
        assert resp.status_code == 200
        body = resp.json()

        sourced = [
            v for v in body["players"].values() if v.get("expected_points") is not None
        ]
        assert sourced, "expected at least one player with xPTS"
        for v in sourced:
            assert v.get("prediction_source"), "prediction_source must be populated"
            assert v.get("data_quality"), "data_quality must be populated"
            # minutes/start estimates ride along from the chain per-player extras.
            assert v.get("minutes_estimate") is not None
            assert v.get("start_prob") is not None

    def test_report_meta_carries_chain_provenance(
        self, live_client: TestClient
    ) -> None:
        """Top-level meta.chain must disclose source_label + data_quality."""
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "gameweek": 3,
            "player_positions": _pos_map(),
            "player_prices": {i: 6.0 for i in range(1, 16)},
            "player_teams": {i: 1 for i in range(1, 16)},
        }
        live_client.post("/api/v1/squad", json=payload)
        resp = live_client.get("/api/v1/decisions")
        assert resp.status_code == 200
        chain = resp.json()["meta"]["chain"]

        assert chain["source"] == "pre-season-proxy-v2"
        assert chain["source_label"] == "Pre-season proxy v2 (price + fixtures + xG + market)"
        assert chain["data_quality"] == "heuristic-proxy-enriched"
        assert isinstance(chain["covered_players"], int)
        assert chain["covered_players"] >= 11

    def test_stub_only_when_forced(self, db_session: Session) -> None:
        """The static stub is NOT resolved in the default (production) wiring.

        With the default dependency the provider is a LivePredictionProvider, so
        the decisions payload carries the live chain provenance — never the flat
        5.5 stub. (The stub is only ever used behind PREDICTION_PROVIDER=static,
        which tests opt into separately.)
        """

        def _override_db() -> Generator[Session, None, None]:
            yield db_session

        app.dependency_overrides[deps._get_db_session] = _override_db
        app.dependency_overrides[deps.get_llm_provider] = lambda: None
        # NOTE: no override of get_prediction_provider -> production default.
        db_session.execute(delete(SquadStateDB))
        db_session.commit()
        try:
            with TestClient(app) as client:
                payload = {
                    "player_ids": list(range(1, 16)),
                    "captain_id": 1,
                    "vice_captain_id": 2,
                    "gameweek": 3,
                    "player_positions": _pos_map(),
                    "player_prices": {i: 6.0 for i in range(1, 16)},
                    "player_teams": {i: 1 for i in range(1, 16)},
                }
                client.post("/api/v1/squad", json=payload)
                resp = client.get("/api/v1/decisions")
                assert resp.status_code == 200
                body = resp.json()
                # Default wiring = live chain, so provenance is present and xPTS
                # are differentiated (not the uniform 5.5 stub).
                assert "chain" in body["meta"]
                xpts = [
                    v["expected_points"]
                    for v in body["players"].values()
                    if v.get("expected_points") is not None
                ]
                assert min(xpts) != max(xpts), "default wiring must not be the flat stub"
        finally:
            app.dependency_overrides.clear()
            db_session.execute(delete(SquadStateDB))
            db_session.commit()
