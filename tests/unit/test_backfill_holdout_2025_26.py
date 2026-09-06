from types import SimpleNamespace

from scripts.backfill_holdout_2025_26 import _canonical_fixture_id_map, _fixture_id_to_int


class _FakeDB:
    def __init__(self, fixtures: list[SimpleNamespace]) -> None:
        self._fixtures = fixtures

    def scalars(self, _statement: object) -> "_FakeDB":
        return self

    def all(self) -> list[SimpleNamespace]:
        return self._fixtures


def test_canonical_fixture_map_does_not_double_hash_provider_id() -> None:
    provider_fixture_id = "9"
    canonical_fixture_id = _fixture_id_to_int(provider_fixture_id)
    fixture = SimpleNamespace(provider_fixture_id=canonical_fixture_id, id=12345)

    mapping = _canonical_fixture_id_map(_FakeDB([fixture]), season_id=1)

    assert mapping[str(canonical_fixture_id)] == 12345
    assert mapping.get(str(_fixture_id_to_int(str(canonical_fixture_id)))) is None

# Keep the latest commit explicitly scoped to the locked-holdout workflow trigger.