"""Phase 9.5 — RSS Connector.

Fetches news from an RSS 2.0 feed and parses each ``<item>`` into a
:class:`~fpl_intelligence.live_intelligence.raw_item_ledger.RawItem`, extracting
the title, content (description / content:encoded), published_at (pubDate) and
permalink (link). It inherits the base connector's rate limiting and typed error
handling.

Parsing uses the stdlib :mod:`xml.etree.ElementTree` — no new dependency — and a
namespace-agnostic child lookup, so feeds that wrap item fields in a default
namespace are handled as easily as plain RSS 2.0. ``pubDate`` is parsed with the
stdlib ``email.utils.parsedate_to_datetime`` (RFC 822/1123); feeds that emit
ISO-8601 timestamps are also accepted. Items without a parseable date are not
dropped: they are treated as published "now", which is the honest minimum. Items
whose ``published_at`` would land in the future (source clock skew) are dropped.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element, ParseError, fromstring

import httpx

from fpl_intelligence.live_intelligence.connectors.base import (
    SourceConnector,
    SourceParseError,
)
from fpl_intelligence.live_intelligence.rate_limit import (
    MonotonicClock,
    SleepFn,
)
from fpl_intelligence.live_intelligence.raw_item_ledger import RawItem
from fpl_intelligence.live_intelligence.source_registry import SourceType
from fpl_intelligence.live_intelligence.temporal_ledger import Clock, utc_now


def _parse_datetime(value: str) -> datetime | None:
    """Parse either an RFC 822/1123 or an ISO-8601 RSS timestamp."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _child_text(node: Element, name: str) -> str:
    """Return trimmed text of the first child whose local tag equals ``name``.

    The lookup ignores XML namespaces by comparing only the local part of the
    tag, so ``<title>`` and ``<{http://...}title>`` are treated identically.
    """
    for child in node:
        local = child.tag.rsplit("}", 1)[-1]
        if local == name and child.text:
            return child.text.strip()
    return ""


class RSSConnector(SourceConnector):
    """Fetch and parse news items from a single RSS feed.

    Args:
        feed_url: Absolute URL of the RSS feed to poll.
        source_id: Phase 9.2 source identifier (defaults to ``"rss"``).
        min_interval_seconds: Minimum delay between successive polls so an
            aggressive scheduler cannot hammer a public feed.
        http_client / clock / monotonic_clock / sleep / timeout / headers:
            Injected test seams (see :class:`SourceConnector`).
    """

    name = "rss"
    source_type = SourceType.RSS

    def __init__(
        self,
        feed_url: str,
        *,
        source_id: str = "rss",
        min_interval_seconds: float = 1.0,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
        http_client: httpx.Client | None = None,
        clock: Clock = utc_now,
        monotonic_clock: MonotonicClock = time.monotonic,
        sleep: SleepFn = time.sleep,
    ) -> None:
        super().__init__(
            http_client=http_client,
            clock=clock,
            monotonic_clock=monotonic_clock,
            sleep=sleep,
            min_interval_seconds=min_interval_seconds,
            timeout=timeout,
            headers=headers,
        )
        self._feed_url = feed_url
        self.source_id = source_id

    @property
    def feed_url(self) -> str:
        return self._feed_url

    def fetch(self, *, limit: int | None = None) -> list[RawItem]:
        response = self._get(self._feed_url)
        try:
            root = fromstring(response.text)
        except ParseError as exc:
            raise SourceParseError(f"invalid RSS XML from {self._feed_url}: {exc}") from exc

        items: list[RawItem] = []
        if root is None:
            return items

        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] != "item":
                continue
            title = _child_text(node, "title") or self.source_id
            description = _child_text(node, "description")
            encoded = _child_text(node, "encoded")
            content = encoded or description
            # A title on its own is not an ingestible article: require some body.
            if not content:
                continue
            raw = self._build_raw_item(
                title=title,
                content_text=content,
                published_at=_parse_datetime(_child_text(node, "pubDate")),
                url=_child_text(node, "link") or None,
                external_id=_child_text(node, "guid") or None,
            )
            if raw is not None:
                items.append(raw)
            if limit is not None and len(items) >= limit:
                break
        return items
