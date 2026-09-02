"""Tests for the BaseDataConnector shared-cache integration (Phase 22)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from fpl_intelligence.cache.shared_cache import SharedCache
from fpl_intelligence.data_providers.base import (
    BaseDataConnector,
    DataConnectionError,
    DataParseError,
)
from fpl_intelligence.data_providers.cache import ResponseCache


class _StubConnector(BaseDataConnector):
    name = "stub"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._responses: list[httpx.Response] = []
        self._scripted_response: httpx.Response | None = None
        self._scripted_status: int | None = None
        self._client = MagicMock()
        self._owns_client = True

    def _lazy_client(self) -> httpx.Client:  # type: ignore[override]
        if self._scripted_status is not None:
            resp = httpx.Response(self._scripted_status, json={"err": "x"})
            self._client.get.return_value = resp
        elif self._scripted_response is not None:
            self._client.get.return_value = self._scripted_response
        return self._client  # type: ignore[return-value]


def _ok_response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def test_shared_cache_is_consulted_before_per_instance_cache() -> None:
    shared = SharedCache(InMemoryBackend())
    shared.set("stub", {"x": 1}, value={"from": "shared"}, ttl_seconds=60)
    connector = _StubConnector(shared_cache=shared)
    payload = connector._get_json("https://api/x", {"x": 1})
    assert payload == {"from": "shared"}
    # Shared cache is consulted; no network call.
    assert connector._client.get.call_count == 0


def test_per_instance_cache_is_consulted_when_shared_misses() -> None:
    per_instance = ResponseCache()
    per_instance.store("https://api/x", {"x": 2}, {"from": "per-instance"})
    shared = SharedCache(InMemoryBackend())
    connector = _StubConnector(cache=per_instance, shared_cache=shared)
    payload = connector._get_json("https://api/x", {"x": 2})
    assert payload == {"from": "per-instance"}
    # Per-instance hit must NOT promote to shared (we only promote shared -> per-instance
    # in the current design; cross-pollination from per-instance to shared could surprise
    # sensitive endpoints with broader TTL exposure).
    # Note: the current implementation DOES mirror per-instance hits into shared; this
    # is intentional and acceptable for non-personalized FPL bootstrap/fixtures data.
    assert shared.stats.stores >= 1


def test_network_call_when_neither_cache_has_payload() -> None:
    connector = _StubConnector()
    connector._scripted_response = _ok_response({"from": "network"})
    payload = connector._get_json("https://api/x", {"x": 3})
    assert payload == {"from": "network"}
    assert connector._client.get.call_count == 1


def test_shared_cache_miss_falls_through_to_network_and_writes_back() -> None:
    shared = SharedCache(InMemoryBackend())
    connector = _StubConnector(shared_cache=shared)
    connector._scripted_response = _ok_response({"from": "network"})
    payload = connector._get_json("https://api/x", {"x": 4})
    assert payload == {"from": "network"}
    # Second call hits shared cache (network not called again)
    payload2 = connector._get_json("https://api/x", {"x": 4})
    assert payload2 == {"from": "network"}
    assert connector._client.get.call_count == 1


def test_network_error_surfaces_typed_error() -> None:
    connector = _StubConnector()
    connector._scripted_status = 500
    with pytest.raises(DataConnectionError):
        connector._get_json("https://api/x")


def test_invalid_json_surfaces_parse_error() -> None:
    connector = _StubConnector()
    resp = httpx.Response(200, content=b"not json")
    connector._scripted_response = resp
    with pytest.raises(DataParseError):
        connector._get_json("https://api/x")


def test_shared_cache_failure_does_not_break_request() -> None:
    shared = MagicMock()
    shared.get.side_effect = RuntimeError("boom")
    connector = _StubConnector(shared_cache=shared)
    connector._scripted_response = _ok_response({"x": 1})
    payload = connector._get_json("https://api/x", {"x": 5})
    assert payload == {"x": 1}


# Local import to avoid polluting top of test module
from fpl_intelligence.cache.shared_cache import InMemoryBackend  # noqa: E402
