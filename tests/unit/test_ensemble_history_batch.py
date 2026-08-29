from __future__ import annotations

from fpl_intelligence.models.ensemble_xpts import collect_player_inputs_batch


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def execute(self, _statement):
        self.calls += 1
        return FakeResult(self.rows)


def test_collect_player_inputs_batch_uses_one_query_and_preserves_history_shape():
    db = FakeSession(
        [
            (10, 1, 2),
            (10, 2, 8),
            (10, 3, 5),
            (20, 1, 4),
            (20, 2, 7),
        ]
    )

    result = collect_player_inputs_batch(db, [10, 20, 10])

    assert db.calls == 1
    assert result[10]["points_history"] == [2.0, 8.0, 5.0]
    assert result[10]["recent_points"] == [5.0, 8.0, 2.0]
    assert result[20]["points_history"] == [4.0, 7.0]
    assert result[20]["recent_points"] == [7.0, 4.0]


def test_collect_player_inputs_batch_handles_empty_ids_without_query():
    db = FakeSession([])

    assert collect_player_inputs_batch(db, []) == {}
    assert db.calls == 0
