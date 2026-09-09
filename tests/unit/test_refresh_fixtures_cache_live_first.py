"""refresh_fixtures_cache: live FPL first, vaastav fallback.

Verifies the fix for the GW2→GW3 staleness bug: vaastav's finished flags
lag by up to 24h, so the daily sync must prefer the live FPL endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GW2_FINISHED = [{"event": 2, "finished": True, "team_h": 1, "team_a": 2}]
_GW3_UPCOMING = [{"event": 3, "finished": False, "team_h": 3, "team_a": 4}]
_LIVE_PAYLOAD = _GW2_FINISHED + _GW3_UPCOMING


def _mock_db():
    db = MagicMock()
    db.execute.return_value = None
    db.add.return_value = None
    db.commit.return_value = None
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_fetch_wins_over_vaastav():
    """When live FPL returns data, it is stored and vaastav is never called."""
    db = _mock_db()
    adapter = AsyncMock()
    adapter.fetch = AsyncMock(return_value=_LIVE_PAYLOAD)

    with (
        patch(
            "fpl_intelligence.data_providers.registry.get_async_fpl_adapter",
            return_value=adapter,
        ),
        patch("fpl_intelligence.materialize.service.fetch_text") as mock_vaastav,
    ):
        from fpl_intelligence.materialize.service import refresh_fixtures_cache

        result = await refresh_fixtures_cache(db, "2026-27")

    assert result["ok"] is True
    assert result["source"] == "fpl-live"
    assert result["fixtures"] == 2
    assert result["next_unfinished_gw"] == 3
    mock_vaastav.assert_not_called()


@pytest.mark.asyncio
async def test_falls_back_to_vaastav_when_live_fails():
    """When the live FPL fetch raises, vaastav is used instead."""
    db = _mock_db()
    adapter = AsyncMock()
    adapter.fetch = AsyncMock(side_effect=RuntimeError("FPL blocked"))

    with (
        patch(
            "fpl_intelligence.data_providers.registry.get_async_fpl_adapter",
            return_value=adapter,
        ),
        patch(
            "fpl_intelligence.materialize.service.fetch_text",
            new_callable=AsyncMock,
            return_value="event,finished\n2,True\n",
        ),
        patch(
            "fpl_intelligence.materialize.service.parse_fixtures_csv",
            return_value=_GW2_FINISHED,
        ),
    ):
        from fpl_intelligence.materialize.service import refresh_fixtures_cache

        result = await refresh_fixtures_cache(db, "2026-27")

    assert result["ok"] is True
    assert result["source"] == "vaastav:2026-27"


@pytest.mark.asyncio
async def test_falls_back_to_vaastav_when_live_returns_empty():
    """An empty live response is treated as a failure — vaastav is tried."""
    db = _mock_db()
    adapter = AsyncMock()
    adapter.fetch = AsyncMock(return_value=[])

    with (
        patch(
            "fpl_intelligence.data_providers.registry.get_async_fpl_adapter",
            return_value=adapter,
        ),
        patch(
            "fpl_intelligence.materialize.service.fetch_text",
            new_callable=AsyncMock,
            return_value="event,finished\n2,True\n",
        ),
        patch(
            "fpl_intelligence.materialize.service.parse_fixtures_csv",
            return_value=_GW2_FINISHED,
        ),
    ):
        from fpl_intelligence.materialize.service import refresh_fixtures_cache

        result = await refresh_fixtures_cache(db, "2026-27")

    assert result["ok"] is True
    assert result["source"] == "vaastav:2026-27"


@pytest.mark.asyncio
async def test_returns_failure_when_both_sources_fail():
    """Both live and vaastav failing → ok=False, nothing written to DB."""
    db = _mock_db()
    adapter = AsyncMock()
    adapter.fetch = AsyncMock(side_effect=RuntimeError("FPL blocked"))

    with (
        patch(
            "fpl_intelligence.data_providers.registry.get_async_fpl_adapter",
            return_value=adapter,
        ),
        patch(
            "fpl_intelligence.materialize.service.fetch_text",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        from fpl_intelligence.materialize.service import refresh_fixtures_cache

        result = await refresh_fixtures_cache(db, "2026-27")

    assert result["ok"] is False
    db.add.assert_not_called()
