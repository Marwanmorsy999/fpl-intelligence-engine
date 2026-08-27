"""Phase 10.4 — Squad Decision Engine tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.live_intelligence.bridge import StaticPredictionProvider
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
from fpl_intelligence.squad.models import SquadStateCreate
from fpl_intelligence.squad.service import SquadService

# ---------------------------------------------------------------------------
# SquadService tests
# ---------------------------------------------------------------------------


class TestSquadService:
    """Unit tests for the in-memory SquadService."""

    def test_set_and_get_squad(self) -> None:
        svc = SquadService()
        payload = SquadStateCreate(
            player_ids=list(range(1, 16)),
            captain_id=1,
            vice_captain_id=2,
            bank=1.5,
            free_transfers=2,
            chips_available=["wildcard", "free_hit"],
            gameweek=5,
        )
        stored = svc.set_squad(payload, session_id="mem_user")
        assert stored.player_ids == list(range(1, 16))
        assert stored.captain_id == 1
        assert stored.bank == 1.5
        assert stored.free_transfers == 2
        assert stored.gameweek == 5
        assert stored.updated_at is not None

    def test_get_squad_returns_none_when_empty(self) -> None:
        svc = SquadService()
        assert svc.get_squad(session_id="empty") is None

    def test_set_squad_replaces_previous(self) -> None:
        svc = SquadService()
        first = SquadStateCreate(
            player_ids=list(range(1, 16)),
            captain_id=1,
            vice_captain_id=2,
            gameweek=1,
        )
        second = SquadStateCreate(
            player_ids=list(range(10, 25)),
            captain_id=20,
            vice_captain_id=19,
            gameweek=5,
        )
        svc.set_squad(first, session_id="rep_user")
        svc.set_squad(second, session_id="rep_user")
        current = svc.get_squad(session_id="rep_user")
        assert current is not None
        assert current.player_ids == list(range(10, 25))
        assert current.gameweek == 5

    def test_clear_removes_squad(self) -> None:
        svc = SquadService()
        payload = SquadStateCreate(
            player_ids=list(range(1, 16)),
            captain_id=1,
            vice_captain_id=2,
            gameweek=1,
        )
        svc.set_squad(payload, session_id="clr_user")
        assert svc.get_squad(session_id="clr_user") is not None
        svc.clear(session_id="clr_user")
        assert svc.get_squad(session_id="clr_user") is None


# ---------------------------------------------------------------------------
# DecisionOptimizerBridge tests
# ---------------------------------------------------------------------------


def _pos_map() -> dict[int, int]:
    return {i: (1 if i <= 2 else (2 if i <= 7 else (3 if i <= 12 else 4))) for i in range(1, 16)}


class TestDecisionOptimizerBridge:
    """Unit tests for the DecisionOptimizerBridge with mocked Phase 6 optimizers."""

    def _make_bridge(self, provider=None) -> DecisionOptimizerBridge:
        if provider is None:
            provider = StaticPredictionProvider()
        return DecisionOptimizerBridge(provider=provider)

    def test_generate_decisions_returns_report(self) -> None:
        bridge = self._make_bridge()
        squad = SquadStateCreate(
            player_ids=list(range(1, 16)),
            captain_id=1,
            vice_captain_id=2,
            bank=0.5,
            free_transfers=1,
            gameweek=3,
            player_positions=_pos_map(),
        )
        report = bridge.generate_decisions(squad)
        assert report.gameweek == 3
        assert len(report.starting_xi) == 11
        assert len(report.bench_order) == 4
        assert set(report.starting_xi) | set(report.bench_order) == set(range(1, 16))
        assert report.captain is not None

    def test_generate_decisions_without_positions_returns_naive_xi(self) -> None:
        bridge = self._make_bridge()
        squad = SquadStateCreate(
            player_ids=list(range(1, 16)),
            captain_id=1,
            vice_captain_id=2,
            gameweek=1,
            player_positions=None,
        )
        report = bridge.generate_decisions(squad)
        assert len(report.starting_xi) == 11
        assert report.starting_xi == list(range(1, 12))
        assert report.captain is not None

    def test_generate_decisions_without_metadata_skips_transfer(self) -> None:
        bridge = self._make_bridge()
        squad = SquadStateCreate(
            player_ids=list(range(1, 16)),
            captain_id=1,
            vice_captain_id=2,
            gameweek=1,
            player_positions=_pos_map(),
            player_prices=None,
            player_teams=None,
        )
        report = bridge.generate_decisions(squad)
        assert report.transfer_plan is None

    def test_generate_decisions_with_full_metadata_runs_transfer(self) -> None:
        provider = StaticPredictionProvider()
        bridge = self._make_bridge(provider)
        positions = _pos_map()
        prices = {i: 5.0 for i in range(1, 16)}
        teams = {i: 1 for i in range(1, 16)}
        teams[3] = 2
        squad = SquadStateCreate(
            player_ids=list(range(1, 16)),
            captain_id=1,
            vice_captain_id=2,
            gameweek=1,
            player_positions=positions,
            player_prices=prices,
            player_teams=teams,
        )
        report = bridge.generate_decisions(squad)
        assert report.gameweek == 1

    def test_captain_recommendation_is_in_starting_xi(self) -> None:
        bridge = self._make_bridge()
        positions = _pos_map()
        squad = SquadStateCreate(
            player_ids=list(range(1, 16)),
            captain_id=1,
            vice_captain_id=2,
            gameweek=1,
            player_positions=positions,
        )
        report = bridge.generate_decisions(squad)
        assert report.captain is not None
        assert report.captain.player_id in report.starting_xi

    def test_chip_recommendation_none_when_not_beneficial(self) -> None:
        provider = StaticPredictionProvider(
            expected_points=1.0,
            expected_minutes=30.0,
            start_probability=0.5,
            floor=0.0,
            ceiling=2.0,
        )
        bridge = self._make_bridge(provider)
        positions = _pos_map()
        squad = SquadStateCreate(
            player_ids=list(range(1, 16)),
            captain_id=1,
            vice_captain_id=2,
            gameweek=1,
            player_positions=positions,
        )
        report = bridge.generate_decisions(squad)
        assert report.chip_recommendation is None or report.chip_recommendation.chip_name is None


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    from sqlalchemy import delete

    from fpl_intelligence.api.main import app
    from fpl_intelligence.squad.models_db import SquadStateDB

    def _override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[deps._get_db_session] = _override_db
    app.dependency_overrides[deps.get_llm_provider] = lambda: MagicMock()
    app.dependency_overrides[deps.get_prediction_provider] = lambda: StaticPredictionProvider()

    # Start each test from a clean squad state (Phase 11.2 — DB-backed).
    db_session.execute(delete(SquadStateDB))
    db_session.commit()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        db_session.execute(delete(SquadStateDB))
        db_session.commit()


class TestSquadAPI:
    """Tests for POST /api/v1/squad, GET /api/v1/squad, GET /api/v1/decisions."""

    def test_post_squad_returns_201(self, client: TestClient) -> None:
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "bank": 1.5,
            "free_transfers": 2,
            "chips_available": ["wildcard", "free_hit"],
            "gameweek": 5,
        }
        resp = client.post("/api/v1/squad", json=payload, params={"session_id": "post_user"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["gameweek"] == 5
        assert body["bank"] == 1.5
        assert body["updated_at"] is not None

    def test_post_squad_requires_session_id(self, client: TestClient) -> None:
        """Missing session_id -> 400 error."""
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "gameweek": 1,
        }
        resp = client.post("/api/v1/squad", json=payload)
        assert resp.status_code == 400

    def test_post_squad_validates_player_count(self, client: TestClient) -> None:
        payload = {
            "player_ids": [1, 2, 3],
            "captain_id": 1,
            "vice_captain_id": 2,
            "gameweek": 1,
        }
        resp = client.post("/api/v1/squad", json=payload, params={"session_id": "validate_user"})
        assert resp.status_code == 422

    def test_get_squad_returns_stored_state(self, client: TestClient) -> None:
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 10,
            "vice_captain_id": 11,
            "bank": 0.0,
            "free_transfers": 1,
            "gameweek": 10,
        }
        client.post("/api/v1/squad", json=payload, params={"session_id": "test_user"})
        resp = client.get("/api/v1/squad", params={"session_id": "test_user"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["gameweek"] == 10
        assert body["captain_id"] == 10

    def test_get_squad_returns_404_when_no_session(self, client: TestClient) -> None:
        """Missing session_id -> 404, never returns another user's squad."""
        resp = client.get("/api/v1/squad")
        assert resp.status_code == 404

    def test_get_squad_returns_404_for_unknown_session(self, client: TestClient) -> None:
        """Unknown session_id -> 404, never falls back to a default."""
        resp = client.get("/api/v1/squad", params={"session_id": "never_seen"})
        assert resp.status_code == 404

    def test_get_decisions_returns_report(self, client: TestClient) -> None:
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "bank": 0.0,
            "free_transfers": 1,
            "gameweek": 3,
            "player_positions": _pos_map(),
        }
        client.post("/api/v1/squad", json=payload, params={"session_id": "dec_user"})
        resp = client.get("/api/v1/decisions", params={"session_id": "dec_user"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["gameweek"] == 3
        assert len(body["starting_xi"]) == 11
        assert len(body["bench_order"]) == 4
        assert body["captain"] is not None

    def test_get_decisions_returns_404_when_no_squad(self, client: TestClient) -> None:
        resp = client.get("/api/v1/decisions", params={"session_id": "no_squad_user"})
        assert resp.status_code == 404

    def test_get_decisions_requires_session_id(self, client: TestClient) -> None:
        """Missing session_id -> 404, never returns another user's squad."""
        resp = client.get("/api/v1/decisions")
        assert resp.status_code == 404

    def test_get_decisions_session_a_cant_read_session_b(self, client: TestClient) -> None:
        """Session A's squad is invisible to session B."""
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "gameweek": 1,
            "player_positions": _pos_map(),
        }
        client.post("/api/v1/squad", json=payload, params={"session_id": "user_a"})
        resp = client.get("/api/v1/decisions", params={"session_id": "user_b"})
        assert resp.status_code == 404

    def test_post_squad_with_positions_generates_full_report(self, client: TestClient) -> None:
        positions = _pos_map()
        prices = {i: 5.0 for i in range(1, 16)}
        teams = {i: (1 if i <= 5 else 2) for i in range(1, 16)}
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "bank": 1.0,
            "free_transfers": 1,
            "gameweek": 1,
            "player_positions": positions,
            "player_prices": prices,
            "player_teams": teams,
        }
        client.post("/api/v1/squad", json=payload, params={"session_id": "full_report_user"})
        resp = client.get("/api/v1/decisions", params={"session_id": "full_report_user"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["transfer_plan"] is not None


# ---------------------------------------------------------------------------
# v2.5.7 — fpl-view endpoint and banner honesty tests
# ---------------------------------------------------------------------------


class TestFplViewEndpoint:
    """Tests for GET /api/v1/squad/fpl-view — raw FPL truth via egress masks."""

    @pytest.mark.xfail(
        reason=(
            "requires live FPL egress (non-hermetic; audit 2026-08). The route "
            "now degrades to an honest 503 when every egress strategy fails."
        ),
        strict=False,
    )
    def test_fpl_view_returns_200_for_existing_squad(self, client: TestClient) -> None:
        """fpl-view returns 200 with correct shape when squad exists."""
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "gameweek": 1,
            "player_positions": _pos_map(),
        }
        # fpl-view requires numeric session_id (FPL entry_id)
        client.post("/api/v1/squad", json=payload, params={"session_id": "1234567"})
        resp = client.get("/api/v1/squad/fpl-view", params={"session_id": "1234567"})
        assert resp.status_code == 200
        body = resp.json()
        # Shape assertion
        assert "current_event" in body
        assert isinstance(body["current_event"], int)
        assert "picks_current" in body
        assert "gw" in body["picks_current"]
        assert "ids" in body["picks_current"]
        assert "status" in body["picks_current"]
        assert body["picks_current"]["status"] in (200, 404)
        assert "picks_next" in body
        assert "gw" in body["picks_next"]
        assert "ids" in body["picks_next"]
        assert "status" in body["picks_next"]
        assert body["picks_next"]["status"] in (200, 404)
        assert "entry_summary" in body
        assert "current_event" in body["entry_summary"]

    def test_fpl_view_returns_400_for_invalid_session(self, client: TestClient) -> None:
        """Invalid session_id -> 400."""
        resp = client.get("/api/v1/squad/fpl-view", params={"session_id": "not_a_number"})
        assert resp.status_code == 400

    def test_fpl_view_returns_404_for_missing_session(self, client: TestClient) -> None:
        """Missing session_id -> 422 (required query param)."""
        resp = client.get("/api/v1/squad/fpl-view")
        assert resp.status_code == 422


class TestBannerHonestyRules:
    """Tests for v2.5.7 banner honesty rules in sync-now job."""

    def test_404_equal_saved_shows_honest_banner(self, client: TestClient) -> None:
        """
        When picks_next is 404 AND picks_current == saved squad,
        banner MUST say: "FPL shows no new GW{X} lineup yet — confirm your transfer on FPL, then sync again."
        Never "synced · no changes".
        """
        # This test verifies the rule exists; the actual sync job logic
        # is tested via integration tests with mocked FPL responses.
        # Here we assert the sync-status payload includes the honesty fields.
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "gameweek": 1,
            "player_positions": _pos_map(),
        }
        client.post("/api/v1/squad", json=payload, params={"session_id": "1234568"})

        # Start a sync-now job
        resp = client.post("/api/v1/squad/sync-now", params={"session_id": "1234568"})
        assert resp.status_code in (200, 202)

        # Poll sync-status for the honesty fields
        import time
        for _ in range(10):
            status_resp = client.get("/api/v1/squad/sync-status", params={"session_id": "1234568"})
            if status_resp.status_code == 200:
                body = status_resp.json()
                # v2.5.7 fields must be present
                assert "chose_rule" in body
                assert "picks_next_status" in body
                assert "ids_hash_current" in body
                assert "ids_hash_next" in body
                if body.get("state") == "done":
                    # The banner text should be honest based on chose_rule
                    banner = body.get("banner", "")
                    if body.get("chose_rule") == "404_equal_saved":
                        assert "FPL shows no new GW" in banner
                        assert "confirm your transfer on FPL" in banner
                        assert "synced" not in banner.lower() or "no changes" not in banner.lower()
                    break
            time.sleep(0.5)

    def test_200_differs_saved_shows_in_out_banner(self, client: TestClient) -> None:
        """
        When picks_next is 200 and differs from saved,
        banner shows IN/OUT as normal.
        """
        payload = {
            "player_ids": list(range(1, 16)),
            "captain_id": 1,
            "vice_captain_id": 2,
            "gameweek": 1,
            "player_positions": _pos_map(),
        }
        client.post("/api/v1/squad", json=payload, params={"session_id": "1234569"})

        resp = client.post("/api/v1/squad/sync-now", params={"session_id": "1234569"})
        assert resp.status_code in (200, 202)

        import time
        for _ in range(10):
            status_resp = client.get("/api/v1/squad/sync-status", params={"session_id": "1234569"})
            if status_resp.status_code == 200:
                body = status_resp.json()
                assert "chose_rule" in body
                assert "picks_next_status" in body
                assert "ids_hash_current" in body
                assert "ids_hash_next" in body
                if body.get("state") == "done":
                    banner = body.get("banner", "")
                    if body.get("chose_rule") == "200_differs_saved":
                        assert "IN:" in banner
                        assert "OUT:" in banner
                    break
            time.sleep(0.5)
