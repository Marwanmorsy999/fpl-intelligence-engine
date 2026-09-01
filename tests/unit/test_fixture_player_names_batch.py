"""Regression tests for batched fixture player-name resolution."""

from __future__ import annotations

from unittest.mock import MagicMock

from fpl_intelligence.api.routes.fixtures import _resolve_player_names


class _ScalarRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> "_ScalarRows":
        return self

    def all(self) -> list[object]:
        return self._rows


def test_player_names_use_batched_element_and_legacy_queries() -> None:
    provider = MagicMock()
    provider.execute.side_effect = [
        _ScalarRows([
            MagicMock(fpl_element_id=101, id=9001, web_name="Alpha"),
            MagicMock(fpl_element_id=202, id=9002, web_name="Beta"),
        ]),
        _ScalarRows([
            MagicMock(fpl_element_id=None, id=9003, web_name="Gamma"),
        ]),
    ]

    names = _resolve_player_names(provider, [101, 202, 101, 9003])

    assert names == {101: "Alpha", 202: "Beta", 9003: "Gamma"}
    assert provider.execute.call_count == 2
    provider.get.assert_not_called()
