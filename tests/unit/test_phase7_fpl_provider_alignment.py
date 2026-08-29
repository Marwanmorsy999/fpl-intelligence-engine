from __future__ import annotations

from fpl_intelligence.availability.historical.entity_resolution import _provider_alias_candidates


def test_real_fpl_namespaces_share_numeric_identity_aliases():
    assert "real_fpl_bootstrap" in _provider_alias_candidates("real_fpl")
    assert "real_fpl" in _provider_alias_candidates("real_fpl_bootstrap")
    assert "fplcache_pit" in _provider_alias_candidates("real_fpl")
    assert "fplcache_pit" in _provider_alias_candidates("real_fpl_bootstrap")
