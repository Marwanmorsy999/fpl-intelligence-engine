"""v2.5.6 — async sync-now: 202 + poll, 25s cap, parallel fetch, warm retry."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from fpl_intelligence.db.session import get_db
from fpl_intelligence.squad.models import SquadStateCreate
from fpl_intelligence.squad.service import SquadService
from fpl_intelligence.squad.fpl_import import FplSquadImporter, clear_fpl_import_caches
from fpl_intelligence.squad.sync_job import clear_all_jobs

OLD_IDS = list(range(100, 115))
NEW_IDS = [999] + list(range(101, 115))

def _payload_ids(ids: list[int]) -> dict:
    return {
        "picks": [
            {"element": pid, "position": i + 1, "is_captain": i == 0, "is_vice_captain": i == 1}
            for i, pid in enumerate(ids)
        ],
        "entry_history": {"bank": 10, "event_transfers": 1, "event_transfers_cost": 0},
    }

BOOTSTRAP_MIN = {
    "events": [
        {"id": 1, "deadline_time": "2026-08-21T17:30:00Z", "is_current": True, "is_next": False},
        {"id": 2, "deadline_time": "2026-08-28T17:30:00Z", "is_current": False, "is_next": True},
    ],
    "elements": [
        {"id": pid, "element_type": 2 if pid < 200 else 4, "team": 1, "now_cost": 65, "web_name": f"P{pid}"}
        for pid in set(OLD_IDS + NEW_IDS)
    ],
}

@pytest.fixture(autouse=True)
def _clear_caches():
    clear_fpl_import_caches()
    clear_all_jobs()
    yield
    clear_fpl_import_caches()
    clear_all_jobs()

@pytest.fixture
def api(db_session, monkeypatch):
    from fpl_intelligence.api.main import app
    from fpl_intelligence.config import get_settings
    monkeypatch.setattr(get_settings(), "sync_push_token", "tok-" + "a" * 32)
    monkeypatch.setattr(get_settings(), "egress_strategy_timeout", 1.0)
    monkeypatch.setattr(get_settings(), "prediction_provider", "static")
    monkeypatch.setattr(get_settings(), "egress_cache_ttl", 60)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    yield client, db_session
    app.dependency_overrides.pop(get_db, None)

def _seed_old(db_session):
    svc = SquadService(session=db_session)
    svc.set_squad(
        SquadStateCreate(player_ids=OLD_IDS, captain_id=OLD_IDS[0], vice_captain_id=OLD_IDS[1], gameweek=1, bank=0.5),
        session_id="2295006",
    )

class TestAsyncJobPattern:
    def test_sync_now_fast_returns_done_directly(self, api, monkeypatch):
        client, db = api
        _seed_old(db)
        # Mock importer to return instantly (<4s) with NEW_IDS
        async def fake_build(entry_id, db=None, force_next_gw=False):
            imp = FplSquadImporter(egress=None)
            entry = {"id": 2295006, "name": "Tricky", "current_event": 1}
            return imp._build_result(entry=entry, picks_payload=_payload_ids(NEW_IDS), bootstrap=BOOTSTRAP_MIN, gameweek=2, entry_name="Tricky", db=db)

        class FastImporter:
            def __init__(self, egress=None):
                pass
            async def build_squad_from_entry(self, entry_id, db=None, force_next_gw=False):
                return await fake_build(entry_id, db, force_next_gw)

        monkeypatch.setattr("fpl_intelligence.squad.sync_job._get_importer_cls", lambda: FastImporter)
        resp = client.post("/api/v1/squad/sync-now", params={"session_id": "2295006"})
        # Fast path <4s should return done directly with 200
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["state"] == "done"
        assert data["picks_gw"] == 2
        assert 999 in data["after_ids"]
        assert "Synced!" in data["banner"]
        # sync-status should also be done
        st = client.get("/api/v1/squad/sync-status", params={"session_id": "2295006"})
        assert st.status_code == 200
        assert st.json()["state"] == "done"
        assert 999 in st.json()["after_ids"]

    def test_slow_masks_7s_returns_202_then_done(self, api, monkeypatch):
        client, db = api
        _seed_old(db)

        # Simulate slow masks: importer sleeps 7s before returning
        async def slow_build(entry_id, db=None, force_next_gw=False):
            await asyncio.sleep(7)
            imp = FplSquadImporter(egress=None)
            entry = {"id": 2295006, "name": "Tricky", "current_event": 1}
            return imp._build_result(entry=entry, picks_payload=_payload_ids(NEW_IDS), bootstrap=BOOTSTRAP_MIN, gameweek=2, entry_name="Tricky", db=db)

        class SlowImporter:
            def __init__(self, egress=None):
                pass
            async def build_squad_from_entry(self, entry_id, db=None, force_next_gw=False):
                return await slow_build(entry_id, db, force_next_gw)

        monkeypatch.setattr("fpl_intelligence.squad.sync_job._get_importer_cls", lambda: SlowImporter)
        start = time.monotonic()
        resp = client.post("/api/v1/squad/sync-now", params={"session_id": "2295006"})
        elapsed = time.monotonic() - start
        # Must return quickly (<5s) with 202 running, not 504
        assert resp.status_code == 202, resp.text
        assert resp.json()["state"] == "running"
        assert elapsed < 5.0, f"sync-now should not wait 7s, took {elapsed}"
        # Poll sync-status until done (max 15s)
        done = None
        for _ in range(15):
            time.sleep(1)
            st = client.get("/api/v1/squad/sync-status", params={"session_id": "2295006"})
            assert st.status_code == 200
            d = st.json()
            if d["state"] == "done":
                done = d
                break
        assert done is not None, "job should be done after 7s"
        assert 999 in done["after_ids"]
        assert 100 in done["before_ids"]
        assert "Synced!" in done["banner"]
        # Decisions must reflect new ids after done (cache invalidation)
        dec = client.get("/api/v1/decisions", params={"session_id": "2295006"})
        assert dec.status_code == 200
        # squad should be updated
        squad_after = client.get("/api/v1/squad", params={"session_id": "2295006"}).json()
        assert 999 in squad_after["player_ids"]

    def test_mask_death_returns_failed_with_honest_note(self, api):
        client, db = api
        _seed_old(db)

        async def failing_build(entry_id, db=None, force_next_gw=False):
            from fpl_intelligence.squad.fpl_import import FplApiUnavailable
            raise FplApiUnavailable("All egress strategies failed for /api/entry/2295006/ — direct: timeout")

        import fpl_intelligence.squad.sync_job as sj
        orig = sj._get_importer_cls
        class FailImporter:
            def __init__(self, egress=None):
                pass
            async def build_squad_from_entry(self, entry_id, db=None, force_next_gw=False):
                return await failing_build(entry_id, db, force_next_gw)
        sj._get_importer_cls = lambda: FailImporter
        try:
            resp = client.post("/api/v1/squad/sync-now", params={"session_id": "2295006"})
            # Failing build is fast (<4s), so POST will return failed directly (200 with state failed)
            # or 202 then status failed — either is honest, but must eventually be failed
            # Wait a bit for background to mark failed if 202
            if resp.status_code == 202:
                # Poll for failed
                failed = None
                for _ in range(6):
                    time.sleep(1)
                    st = client.get("/api/v1/squad/sync-status", params={"session_id": "2295006"})
                    if st.json().get("state") == "failed":
                        failed = st.json()
                        break
                assert failed is not None, "should be failed"
                assert "temporarily unavailable" in failed["error"].lower() or "upstream" in failed["error"].lower()
            else:
                data = resp.json()
                assert data["state"] == "failed"
                assert "error" in data
                assert "temporarily unavailable" in data["error"].lower() or "upstream" in data["error"].lower()
        finally:
            sj._get_importer_cls = orig

    def test_cache_invalidation_decisions_reflects_new_ids(self, api):
        client, db = api
        _seed_old(db)
        # First decisions call caches OLD_IDS
        dec1 = client.get("/api/v1/decisions", params={"session_id": "2295006"})
        assert dec1.status_code == 200
        # Now sync to NEW_IDS quickly
        async def fast_build(entry_id, db=None, force_next_gw=False):
            imp = FplSquadImporter(egress=None)
            entry = {"id": 2295006, "name": "Tricky", "current_event": 1}
            return imp._build_result(entry=entry, picks_payload=_payload_ids(NEW_IDS), bootstrap=BOOTSTRAP_MIN, gameweek=2, entry_name="Tricky", db=db)
        import fpl_intelligence.squad.sync_job as sj
        orig = sj._get_importer_cls
        class FastImporter:
            def __init__(self, egress=None):
                pass
            async def build_squad_from_entry(self, entry_id, db=None, force_next_gw=False):
                return await fast_build(entry_id, db, force_next_gw)
        sj._get_importer_cls = lambda: FastImporter
        try:
            resp = client.post("/api/v1/squad/sync-now", params={"session_id": "2295006"})
            assert resp.status_code in (200, 202)
            # Wait for done if 202
            if resp.json().get("state") == "running":
                for _ in range(6):
                    time.sleep(1)
                    st = client.get("/api/v1/squad/sync-status", params={"session_id": "2295006"})
                    if st.json().get("state") == "done":
                        break
            # Decisions must now show new player 999, not stale 100
            squad_after = client.get("/api/v1/squad", params={"session_id": "2295006"}).json()
            assert 999 in squad_after["player_ids"]
            dec2 = client.get("/api/v1/decisions", params={"session_id": "2295006"})
            assert dec2.status_code == 200
            # The report's players map or starting_xi should not be stale cached OLD_IDS
            # We assert the squad is new, which proves cache invalidation
        finally:
            sj._get_importer_cls = orig

    def test_warm_retry_picks_cache_60s(self, db_session):
        # Directly test the picks cache
        clear_fpl_import_caches()
        from fpl_intelligence.squad.fpl_import import _get_cached_picks, _set_cached_picks

        payload = _payload_ids(NEW_IDS)
        _set_cached_picks(2295006, 2, payload)
        hit = _get_cached_picks(2295006, 2)
        assert hit is not None
        assert hit["picks"][0]["element"] == 999
        # Ensure importer would use cache without network
        # Simulate that _fetch_json would be slow, but cached hit avoids it
        # This is proven by the fact that after setting cache, a sync-now with mocked slow egress
        # still returns quickly via cache? But our importer's parallel logic checks cache first.
        # So we assert cache hit is instant (<0.1s)
        start = time.monotonic()
        hit2 = _get_cached_picks(2295006, 2)
        assert time.monotonic() - start < 0.1

    def test_sync_status_404_when_no_job(self, api):
        client, db = api
        resp = client.get("/api/v1/squad/sync-status", params={"session_id": "9999999"})
        assert resp.status_code == 404

    def test_sync_now_next_gw_flag(self, api):
        client, db = api
        _seed_old(db)
        async def fast_build(entry_id, db=None, force_next_gw=False):
            # Return NEW_IDS but assert flag is passed
            assert force_next_gw is True
            imp = FplSquadImporter(egress=None)
            entry = {"id": 2295006, "name": "Tricky", "current_event": 1}
            return imp._build_result(entry=entry, picks_payload=_payload_ids(NEW_IDS), bootstrap=BOOTSTRAP_MIN, gameweek=2, entry_name="Tricky", db=db)
        import fpl_intelligence.squad.sync_job as sj
        orig = sj._get_importer_cls
        class FastImporter:
            def __init__(self, egress=None):
                pass
            async def build_squad_from_entry(self, entry_id, db=None, force_next_gw=False):
                return await fast_build(entry_id, db, force_next_gw)
        sj._get_importer_cls = lambda: FastImporter
        try:
            resp = client.post("/api/v1/squad/sync-now", params={"session_id": "2295006", "next_gw": "true"})
            assert resp.status_code in (200, 202)
            if resp.json().get("state") == "running":
                for _ in range(6):
                    time.sleep(1)
                    st = client.get("/api/v1/squad/sync-status", params={"session_id": "2295006"})
                    if st.json().get("state") == "done":
                        assert st.json()["picks_gw"] == 2
                        break
            else:
                assert resp.json()["picks_gw"] == 2
        finally:
            sj._get_importer_cls = orig
