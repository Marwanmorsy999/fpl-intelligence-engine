from types import SimpleNamespace

from scripts.backfill_holdout_2025_26 import _canonical_fixture_id_map, _fixture_id_to_int


def test_canonical_fixture_map_does_not_double_hash_provider_id() -> None:
    provider_fixture_id = "9"
    canonical_fixture_id = _fixture_id_to_int(provider_fixture_id)
    fixtures = [SimpleNamespace(provider_fixture_id=canonical_fixture_id, id=12345)]

    mapping = _canonical_fixture_id_map(type("DB", (), {})(), 1)
