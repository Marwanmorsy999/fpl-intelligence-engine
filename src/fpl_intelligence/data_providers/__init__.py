"""Phase 11.1 — API-first structured data integration.

Brings reliable football/FPL facts into the engine from structured public APIs,
so core facts (injuries, chance of playing, confirmed lineups, formations) no
longer depend on the heavy LLM extraction layer for the decision-critical path.

Connectors (all cache-first, all offline-testable via ``httpx.MockTransport``):

* :class:`FplOfficialConnector` — official FPL API, no key required.
* :class:`ApiFootballConnector` — keyed via ``API_FOOTBALL_KEY``; disables
  gracefully when the key is absent.
* :class:`FootballDataOrgConnector` — keyed via ``FOOTBALL_DATA_ORG_KEY``;
  disables gracefully when the key is absent.

Fact plumbing:

* :class:`ResponseCache` — endpoint+params TTL cache (15 min general / 1 min
  deadline-sensitive).
* :class:`LiveFactInjector` — structured facts -> hard :class:`FactOverride`s.
* :class:`FactOverrideProvider` — wraps the baseline quantitative provider and
  applies overrides without mutating any Phase 1–8 model.
* :class:`FactCollectionService` — wires connectors + injector together.
"""

from __future__ import annotations

from fpl_intelligence.data_providers.api_football import (
    ApiFootballConnector,
    parse_injuries,
    parse_lineups,
)
from fpl_intelligence.data_providers.base import (
    BaseDataConnector,
    DataConnectionError,
    DataConnectorError,
    DataParseError,
    DataProviderDisabledError,
)
from fpl_intelligence.data_providers.cache import (
    DEFAULT_TTL_SECONDS,
    SENSITIVE_TTL_SECONDS,
    CacheStats,
    ResponseCache,
)
from fpl_intelligence.data_providers.decision_bridge import (
    FactCollectionService,
    FactOverrideProvider,
    index_overrides,
)
from fpl_intelligence.data_providers.facts import (
    FactConfidence,
    FactOverride,
    FactSource,
    PlayerFact,
)
from fpl_intelligence.data_providers.football_data_org import (
    Competition,
    FootballDataOrgConnector,
    Match,
    StandingRow,
    parse_competitions,
    parse_matches,
    parse_standings,
)
from fpl_intelligence.data_providers.fpl_official import (
    FPL_BOOTSTRAP_URL,
    FPL_FIXTURES_URL,
    FplOfficialConnector,
)
from fpl_intelligence.data_providers.live_fact_injector import (
    LiveFactInjector,
    LiveFactResult,
)
from fpl_intelligence.data_providers.open_meteo import OpenMeteoConnector
from fpl_intelligence.data_providers.registry import (
    AsyncFplProviderAdapter,
    FplProviderAdapter,
    ProviderBudget,
    ProviderHealthState,
    ProviderMetadata,
    ProviderRegistry,
    ProviderResult,
    ProviderState,
    build_async_fpl_registry,
    build_default_registry,
    fpl_ingestion_adapter,
    get_async_fpl_adapter,
)

__all__ = [
    "ApiFootballConnector",
    "AsyncFplProviderAdapter",
    "BaseDataConnector",
    "CacheStats",
    "Competition",
    "DataConnectionError",
    "DataConnectorError",
    "DataParseError",
    "DataProviderDisabledError",
    "DEFAULT_TTL_SECONDS",
    "FactCollectionService",
    "FactConfidence",
    "FactOverride",
    "FactOverrideProvider",
    "FactSource",
    "FootballDataOrgConnector",
    "FplOfficialConnector",
    "FplProviderAdapter",
    "FPL_BOOTSTRAP_URL",
    "FPL_FIXTURES_URL",
    "index_overrides",
    "LiveFactInjector",
    "LiveFactResult",
    "Match",
    "OpenMeteoConnector",
    "parse_competitions",
    "parse_injuries",
    "parse_lineups",
    "parse_matches",
    "parse_standings",
    "PlayerFact",
    "build_default_registry",
    "build_async_fpl_registry",
    "get_async_fpl_adapter",
    "ProviderMetadata",
    "ProviderBudget",
    "ProviderHealthState",
    "ProviderRegistry",
    "ProviderResult",
    "ProviderState",
    "ResponseCache",
    "SENSITIVE_TTL_SECONDS",
    "StandingRow",
    "fpl_ingestion_adapter",
]
