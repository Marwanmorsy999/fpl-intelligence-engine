from __future__ import annotations

import asyncio

from fpl_intelligence.data_providers.fpl_egress import FplEgressChain

def test_recent_stale_cache_survives_provider_exhaustion():
    now = 1000.0
    chain = FplEgressChain('https://example.invalid', cache_ttl=60, stale_if_error_ttl=900, monotonic_clock=lambda: now)
    chain._cache['/bootstrap'] = (900.0, {'elements': [{'id': 1}]})

    async def fail(_url):
        raise RuntimeError('upstream unavailable')

    chain._strategies = lambda: [('direct', fail)]
    result = asyncio.run(chain.fetch('/bootstrap'))
    assert result == {'elements': [{'id': 1}]}
    assert chain.winning_strategy == 'stale-cache'
