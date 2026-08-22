"""Phase 9.5 — Live Source Connectors.

Automates the ingestion of news from live sources. A :class:`SourceConnector`
fetches raw items from a single source and returns them as Phase 9.2
:class:`~fpl_intelligence.live_intelligence.raw_item_ledger.RawItem` objects; a
:class:`ConnectorScheduler` orchestrates many connectors on demand or on a
schedule and forwards items to the Phase 9.2 ingestion pipeline.

Shipped connectors:

* :class:`RSSConnector` — reads news from an RSS 2.0 feed.
* :class:`FPLAPIConnector` — reads player news / availability risk from the
  official FPL ``bootstrap-static`` endpoint.

This layer is additive: it does not modify the quantitative Phases 1–8 stack,
makes **no** live API calls inside ``pytest`` (tests inject ``httpx`` mock
transports), hardcodes no API keys, and performs no aggressive scraping.
"""

from __future__ import annotations

from fpl_intelligence.live_intelligence.connectors.base import (
    SourceConnectionError,
    SourceConnector,
    SourceConnectorError,
    SourceParseError,
)
from fpl_intelligence.live_intelligence.connectors.fpl_api import (
    FPL_BOOTSTRAP_URL,
    FPLAPIConnector,
)
from fpl_intelligence.live_intelligence.connectors.rss import RSSConnector
from fpl_intelligence.live_intelligence.connectors.scheduler import (
    ConnectorRunStats,
    ConnectorScheduler,
    SchedulerReport,
)

__all__ = [
    "ConnectorRunStats",
    "ConnectorScheduler",
    "FPLAPIConnector",
    "FPL_BOOTSTRAP_URL",
    "RSSConnector",
    "SchedulerReport",
    "SourceConnector",
    "SourceConnectorError",
    "SourceConnectionError",
    "SourceParseError",
]
