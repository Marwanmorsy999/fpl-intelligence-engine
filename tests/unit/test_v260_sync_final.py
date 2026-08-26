"""v2.6.0-sync-final — truth branches: save-next, rebuild-from-history, honest banner."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fpl_intelligence.squad.fpl_import import FplSquadImporter
from fpl_intelligence.squad.fpl_truth import (
    classify_picks_error,
    fetch_fpl_truth,
    history_note,
    rebuild_squad_ids_from_swaps,
)
from fpl_intelligence.squad.models import SquadStateCreate
from fpl_intelligence.squad.service import SquadService

OLD_IDS = list(range(100, 115))  # 15 old players
NEW_IDS = [999] + list(range(101, 115))  # one transfer: 100 -> 999

BOOTSTRAP_MIN = {
    "events": [
        {"id": 1, "deadline_time": "2026-08-21T17:30:00Z", "is_current": True, "is_next": False},
        {"id": 2, "deadline_time": "2026-08-28T17:30:00Z", "is_current": False, "is_next": True},
    ],
    "elements": [
        {"id": pid, "element_type": 4 if pid >= 200 else 3, "team": 1,
         "now_cost": 65, "web_name": f"P{pid}"}
        for pid in sorted(set(OLD_IDS + NEW_IDS))
    ],
}


def _payload_ids(ids: list[int]) -> dict:
    return {
        "picks": [
            {"element": pid, "position": i + 1, "is_captain": i == 0, "is_vice_captain": i == 1}
            for i, pid in enumerate(ids)
        ],
        "entry_history": {"bank": 10, "event_transfers": 0, "event_transfers_cost": 0},
    }


class TestClassifyPicksError:
    def test_typed_404(self) -> None:
        from fpl_intelligence.squad.fpl_import import FplPicksNotSaved

        assert classify_picks_error(FplPicksNotSaved("Picks not saved yet"), "/x") == 404

    def test_attempts_text_404(self) -> None:
        exc = RuntimeError("All egress strategies failed — direct: 404 Not Found")
        assert classify_picks_error(exc, "/x") == 404

    def test_network_error_is_500_not_404(self) -> None:
        """Regression: an error whose text merely mentions 'picks' is NOT a 404."""
        exc = TimeoutError("timeout fetching /api/entry/1/event/2/picks/")
        assert classify_picks_error(exc, "/x") == 500


class TestRebuildSwaps:
    def test_apply_single_swap(self) -> None:
        out = rebuild_squad_ids_from_swaps(
            list(OLD_IDS), [{"element_in": 999, "element_out": 100}]
        )
        assert out is not None
        new_ids, ins, outs = out
        assert 999 in new_ids and 100 not in new_ids
        assert len(new_ids) == 15
        assert ins == [999] and outs == [100]

    def test_unknown_out_player_rejected(self) -> None:
        swaps = [{"element_in": 999, "element_out": 777}]
        assert rebuild_squad_ids_from_swaps(list(OLD_IDS), swaps) is None

    def test_empty_swaps_rejected(self) -> None:
        assert rebuild_squad_ids_from_swaps(list(OLD_IDS), []) is None


class TestHistoryNote:
    def _truth(self, **kw: Any):
        from fpl_intelligence.squad.fpl_truth import FplTruth

        base = dict(current_event=1, next_gw=2)
        base.update(kw)
        return FplTruth(**base)

    def test_row_present_one_transfer(self) -> None:
        t = self._truth(history_row={"event": 2, "event_transfers": 1})
        assert history_note(t) == "FPL history: 1 transfer made for GW2"

    def test_no_row_yet_with_latest(self) -> None:
        t = self._truth(latest_history_row={"event": 1, "event_transfers": 0})
        note = history_note(t)
        assert "no GW2 row yet" in note and "GW1: 0 transfers" in note


class TestBranchA_SaveNextWhenDiffers:
    @pytest.mark.asyncio
    async def test_sync_saves_new_player_when_next_differs(self, db_session) -> None:
        """Branch A proof: picks_next 200 + differs -> saved squad has new id."""
        svc = SquadService(session=db_session)
        svc.set_squad(
            SquadStateCreate(
                player_ids=OLD_IDS,
                captain_id=OLD_IDS[0],
                vice_captain_id=OLD_IDS[1],
                gameweek=1,
                bank=0.0,
            ),
            session_id="260001",
        )
        imp = FplSquadImporter(egress=None)

        async def fake_fetch(path: str, validator=None, use_cache: bool = True):
            if path == "/api/entry/260001/":
                return {"id": 260001, "name": "T", "current_event": 1}
            if path == "/api/bootstrap-static/":
                return BOOTSTRAP_MIN
            if path == "/api/entry/260001/event/1/picks/":
                return _payload_ids(OLD_IDS)
            if path == "/api/entry/260001/event/2/picks/":
                return _payload_ids(NEW_IDS)
            raise AssertionError(f"unexpected {path}")

        imp._fetch_json = fake_fetch  # type: ignore[method-assign]
        result = await asyncio.wait_for(
            imp.build_squad_from_entry(260001, db=db_session), timeout=10
        )
        assert result.gameweek == 2
        assert not result.rebuilt_from_history
        assert 999 in result.squad.player_ids and 100 not in result.squad.player_ids


class TestBranchB_RebuildFromHistory:
    @pytest.mark.asyncio
    async def test_picks404_but_history_has_swap_rebuilds(self, db_session) -> None:
        svc = SquadService(session=db_session)
        svc.set_squad(
            SquadStateCreate(
                player_ids=OLD_IDS,
                captain_id=OLD_IDS[0],
                vice_captain_id=OLD_IDS[1],
                gameweek=1,
                bank=0.0,
            ),
            session_id="260002",
        )
        imp = FplSquadImporter(egress=None)

        async def fake_fetch(path: str, validator=None, use_cache: bool = True):
            if path.endswith("/history/"):
                return {"history": [
                    {"event": 1, "event_transfers": 0, "transfers": []},
                    {"event": 2, "event_transfers": 1, "transfers": [
                        {"id": 7, "element_in": 999, "element_out": 100, "event_cost": 0}
                    ]},
                ]}
            if path.endswith("/transfers/"):
                return []
            if path == "/api/entry/260002/":
                return {"id": 260002, "name": "B", "current_event": 1}
            if path == "/api/bootstrap-static/":
                return BOOTSTRAP_MIN
            if path == "/api/entry/260002/event/1/picks/":
                return _payload_ids(OLD_IDS)
            if path == "/api/entry/260002/event/2/picks/":
                from fpl_intelligence.squad.fpl_import import FplPicksNotSaved

                raise FplPicksNotSaved("Picks not saved")
            raise AssertionError(f"unexpected {path}")

        imp._fetch_json = fake_fetch  # type: ignore[method-assign]
        result = await asyncio.wait_for(
            imp.build_squad_from_entry(260002, db=db_session), timeout=15
        )
        assert result.rebuilt_from_history is True
        assert result.gameweek == 2
        assert 999 in result.squad.player_ids and 100 not in result.squad.player_ids
        assert len(result.squad.player_ids) == 15


class TestBranchC_NoConfirmedTransfer:
    @pytest.mark.asyncio
    async def test_picks404_and_zero_transfers_flags_honest(self, db_session) -> None:
        svc = SquadService(session=db_session)
        svc.set_squad(
            SquadStateCreate(
                player_ids=OLD_IDS,
                captain_id=OLD_IDS[0],
                vice_captain_id=OLD_IDS[1],
                gameweek=1,
                bank=0.0,
            ),
            session_id="260003",
        )
        imp = FplSquadImporter(egress=None)

        async def fake_fetch(path: str, validator=None, use_cache: bool = True):
            if path.endswith("/history/"):
                return {"history": [{"event": 1, "event_transfers": 0, "transfers": []}]}
            if path.endswith("/transfers/"):
                return []
            if path == "/api/entry/260003/":
                return {"id": 260003, "name": "C", "current_event": 1}
            if path == "/api/bootstrap-static/":
                return BOOTSTRAP_MIN
            if path == "/api/entry/260003/event/1/picks/":
                return _payload_ids(OLD_IDS)
            if path == "/api/entry/260003/event/2/picks/":
                from fpl_intelligence.squad.fpl_import import FplPicksNotSaved

                raise FplPicksNotSaved("Picks not saved")
            raise AssertionError(f"unexpected {path}")

        imp._fetch_json = fake_fetch  # type: ignore[method-assign]
        result = await asyncio.wait_for(
            imp.build_squad_from_entry(260003, db=db_session), timeout=15
        )
        assert result.no_pending_transfer is True
        assert result.pending_transfer_gw == 2
        assert result.gameweek == 1  # keeps current truth, no invention


class TestTruthLens:
    @pytest.mark.asyncio
    async def test_fetch_fpl_truth_shapes(self) -> None:
        imp = FplSquadImporter(egress=None)

        async def fake_fetch(path: str, validator=None, use_cache: bool = True):
            if path.endswith("/history/"):
                return {"history": [{"event": 1, "event_transfers": 0, "transfers": []}]}
            if path.endswith("/transfers/"):
                return [{"event": 2, "element_in": 999, "element_out": 100, "event_cost": 4}]
            if path == "/api/entry/9/":
                return {"id": 9, "name": "L", "current_event": 1}
            if path == "/api/bootstrap-static/":
                return BOOTSTRAP_MIN
            if path == "/api/entry/9/event/1/picks/":
                return _payload_ids(OLD_IDS)
            if path == "/api/entry/9/event/2/picks/":
                from fpl_intelligence.squad.fpl_import import FplPicksNotSaved

                raise FplPicksNotSaved("nope")
            raise AssertionError(f"unexpected {path}")

        imp._fetch_json = fake_fetch  # type: ignore[method-assign]
        truth = await fetch_fpl_truth(9, imp)
        assert truth.current_event == 1
        assert truth.next_gw == 2
        assert truth.picks_next_status == 404
        assert truth.next_transfers_count == 1
        assert history_note(truth).startswith("FPL history: no GW2 row yet")


class TestFplViewHistoryField:
    def test_fpl_view_contract_mentions_fpl_history(self) -> None:
        from fpl_intelligence.squad.models import FplViewResponse

        fields = FplViewResponse.model_fields
        assert "fpl_history" in fields

class TestRealHistoryShape:
    """Regression: FPL returns {current, past, chips} - never a 'history' key."""

    @pytest.mark.asyncio
    async def test_fetch_history_reads_current_key(self) -> None:
        from fpl_intelligence.squad.fpl_truth import _fetch_history

        class FakeImp:
            async def _fetch_json(self, path, *, validator=None):
                assert validator is not None
                # The REAL FPL shape:
                validator({"current": [
                    {"event": 1, "event_transfers": 0, "event_transfers_cost": 0}
                ], "past": [], "chips": []})
                return {
                    "current": [{"event": 1, "event_transfers": 0}],
                    "past": [],
                    "chips": [],
                }

        rows = await _fetch_history(FakeImp(), 2295006)  # type: ignore[arg-type]
        assert rows and rows[0]["event"] == 1

    def test_parse_history_transfers_current_shape(self) -> None:
        from fpl_intelligence.transfers.service import parse_history_transfers

        rows = parse_history_transfers(
            {"current": [{"event": 1, "event_transfers": 0}], "past": []}
        )
        assert rows == []