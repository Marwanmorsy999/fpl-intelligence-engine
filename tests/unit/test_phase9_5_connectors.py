"""Phase 9.5 unit tests — Live Source Connectors.

Covers ``RSSConnector`` and ``FPLAPIConnector`` (each with a mocked HTTP
response, so no live network call is made) and ``ConnectorScheduler`` (the full
fetch -> sink orchestration, error isolation, manual + scheduled execution).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from fpl_intelligence.live_intelligence.connectors import (
    ConnectorScheduler,
    FPLAPIConnector,
    RSSConnector,
    SourceConnectionError,
    SourceConnector,
    SourceConnectorError,
    SourceParseError,
)
from fpl_intelligence.live_intelligence.raw_item_ledger import RawItem

NOW = datetime(2025, 8, 16, 12, 0, 0, tzinfo=UTC)


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Salah injury doubt</title>
      <description>Mohamed Salah missed training on Thursday.</description>
      <link>https://example.com/salah</link>
      <guid>guid-1</guid>
      <pubDate>Thu, 14 Aug 2025 09:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Haaland back in training</title>
      <description>Erling Haaland returned to full training.</description>
      <link>https://example.com/haaland</link>
      <guid>guid-2</guid>
      <pubDate>&lt;bad&gt;unparseable&lt;/bad&gt;</pubDate>
    </item>
    <item>
      <title>No content item</title>
      <link>https://example.com/empty</link>
      <pubDate>Thu, 14 Aug 2025 09:05:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Build an httpx.Client whose transport is entirely mocked (no network)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_rss() -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=RSS_XML,
            headers={"Content-Type": "text/xml"},
        )

    return handler


def _noop_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# RSSConnector
# ---------------------------------------------------------------------------


class TestRSSConnector:
    def _connector(self, *, handler=None, **kwargs: Any) -> RSSConnector:
        h = handler or _ok_rss()
        return RSSConnector(
            "https://example.com/feed.rss",
            http_client=make_client(h),
            clock=lambda: NOW,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
            **kwargs,
        )

    def test_fetch_parses_title_content_url_published(self):
        connector = self._connector()
        items = connector.fetch()
        assert len(items) == 2
        first = items[0]
        assert isinstance(first, RawItem)
        assert first.source_id == "rss"
        assert first.title == "Salah injury doubt"
        assert "missed training" in first.content_text
        assert first.url == "https://example.com/salah"
        assert first.external_id == "guid-1"
        assert first.published_at == datetime(2025, 8, 14, 9, 0, tzinfo=UTC)

    def test_fetch_unparseable_pubdate_uses_fetch_time(self):
        connector = self._connector()
        items = connector.fetch()
        second = items[1]
        # The pubDate was garbage "<bad>unparseable</bad>" -> parsed None ->
        # _build_raw_item falls back to the injected clock.
        assert second.title == "Haaland back in training"
        assert second.published_at == NOW

    def test_fetch_skips_item_without_content(self):
        connector = self._connector()
        items = connector.fetch()
        titles = {i.title for i in items}
        assert "No content item" not in titles

    def test_fetch_sets_distinct_content_hashes(self):
        connector = self._connector()
        items = connector.fetch()
        assert len({i.content_hash for i in items}) == len(items)

    def test_fetch_invalid_xml_raises_parse_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<not-valid-xml", headers={"Content-Type": "text/xml"})

        connector = self._connector(handler=handler)
        with pytest.raises(SourceParseError):
            connector.fetch()

    def test_fetch_http_500_raises_connection_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        connector = self._connector(handler=handler)
        with pytest.raises(SourceConnectionError):
            connector.fetch()

    def test_fetch_429_reports_connection_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429)

        connector = self._connector(handler=handler)
        with pytest.raises(SourceConnectionError):
            connector.fetch()

    def test_fetch_network_failure_wrapped(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        connector = self._connector(handler=handler)
        with pytest.raises(SourceConnectorError):
            connector.fetch()

    def test_future_pubdate_item_is_dropped(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0">
          <channel><item>
            <title>Clock skew</title>
            <description>Published in the future relative to our clock.</description>
            <pubDate>Fri, 15 Aug 2025 09:00:00 GMT</pubDate>
          </item></channel>
        </rss>"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})

        connector = RSSConnector(
            "https://example.com/skew.rss",
            http_client=make_client(handler),
            clock=lambda: _utc(2025, 8, 14),
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        assert connector.fetch() == []

    def test_rate_limiter_paces_between_fetches(self):
        sleeps: list[float] = []
        connector = RSSConnector(
            "https://example.com/feed.rss",
            http_client=make_client(_ok_rss()),
            clock=lambda: NOW,
            monotonic_clock=lambda: 10.0,
            sleep=lambda s: sleeps.append(s),
            min_interval_seconds=3.0,
        )
        connector.fetch()
        connector.fetch()
        assert sleeps, "second fetch should have been paced"
        assert connector.rate_limiter.stats.calls == 2

    def test_namespaced_fields_are_parsed(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0" xmlns="http://example.com/ns">
          <channel><item>
            <title>Arsenal team news</title>
            <description>Arteta confirms three injuries.</description>
            <link>https://example.com/arsenal</link>
            <pubDate>Thu, 14 Aug 2025 09:00:00 GMT</pubDate>
          </item></channel>
        </rss>"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})

        connector = RSSConnector(
            "https://example.com/ns.rss",
            http_client=make_client(handler),
            clock=lambda: NOW,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        items = connector.fetch()
        assert len(items) == 1
        assert items[0].title == "Arsenal team news"

    def test_iso8601_pubdate_accepted(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0">
          <channel><item>
            <title>ISO date</title>
            <description>Body.</description>
            <pubDate>2025-08-14T09:00:00Z</pubDate>
          </item></channel>
        </rss>"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=xml, headers={"Content-Type": "text/xml"})

        connector = RSSConnector(
            "https://example.com/iso.rss",
            http_client=make_client(handler),
            clock=lambda: NOW,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        items = connector.fetch()
        assert len(items) == 1
        assert items[0].published_at == datetime(2025, 8, 14, 9, 0, tzinfo=UTC)

    def test_empty_feed_returns_no_items(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<?xml version='1.0'?><rss version='2.0'><channel/></rss>",
                headers={"Content-Type": "text/xml"},
            )

        connector = RSSConnector(
            "https://example.com/empty.rss",
            http_client=make_client(handler),
            clock=lambda: NOW,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        assert connector.fetch() == []

    def test_custom_source_id_is_carried(self):
        connector = self._connector(source_id="bbc_football_rss")
        items = connector.fetch()
        assert items
        assert items[0].source_id == "bbc_football_rss"


# ---------------------------------------------------------------------------
# FPLAPIConnector
# ---------------------------------------------------------------------------


def _fpl_payload() -> dict[str, Any]:
    return {
        "elements": [
            {"id": 411, "web_name": "Salah", "first_name": "Mohamed", "second_name": "Salah",
             "news": "Mohamed Salah is a doubt for this weekend's match.",
             "chance_of_playing_next_round": 75, "chance_of_playing_this_round": 75},
            {"id": 615, "web_name": "Palmer", "first_name": "Cole", "second_name": "Palmer",
             "news": "", "chance_of_playing_next_round": 25, "chance_of_playing_this_round": None},
            {"id": 859, "web_name": "Haaland", "first_name": "Erling", "second_name": "Haaland",
             "news": "", "chance_of_playing_next_round": 100, "chance_of_playing_this_round": 100},
        ]
    }


class TestFPLAPIConnector:
    def _connector(self, *, handler=None, **kwargs: Any) -> FPLAPIConnector:
        def default(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_fpl_payload())

        return FPLAPIConnector(
            api_url="https://example.com/bootstrap-static/",
            http_client=make_client(handler or default),
            clock=lambda: NOW,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
            **kwargs,
        )

    def test_fetch_extracts_news_item(self):
        items = self._connector().fetch()
        salah = next(i for i in items if i.external_id == "411")
        assert "doubt" in salah.content_text
        assert salah.title == "Salah"
        assert salah.url == "https://example.com/bootstrap-static/"
        assert salah.external_id == "411"

    def test_fetch_extracts_chance_of_playing_item(self):
        items = self._connector().fetch()
        palmer = next(i for i in items if i.external_id == "615")
        assert "chance_of_playing_next_round=25%" in palmer.content_text
        assert "availability risk" in palmer.title

    def test_fetch_skips_fully_available_player_no_news(self):
        items = self._connector().fetch()
        ids = {i.external_id for i in items}
        assert "859" not in ids

    def test_fetch_respects_limit(self):
        items = self._connector().fetch(limit=1)
        assert len(items) == 1

    def test_fetch_invalid_json_raises_parse_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json")

        with pytest.raises(SourceParseError):
            self._connector(handler=handler).fetch()

    def test_fetch_missing_elements_raises_parse_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"foo": 1})

        with pytest.raises(SourceParseError):
            self._connector(handler=handler).fetch()

    def test_fetch_http_500_raises_connection_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with pytest.raises(SourceConnectionError):
            self._connector(handler=handler).fetch()

    def test_fetch_network_failure_wrapped(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        with pytest.raises(SourceConnectorError):
            self._connector(handler=handler).fetch()

    def test_fetch_uses_web_name_fallback_to_full_name(self):
        payload = {"elements": [
            {"id": 1, "web_name": "", "first_name": "Kai", "second_name": "Havertz",
             "news": "Fit again.", "chance_of_playing_next_round": 90,
             "chance_of_playing_this_round": 90}
        ]}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        items = self._connector(handler=handler).fetch()
        assert len(items) == 1
        assert items[0].external_id == "1"

    def test_fetch_accepts_string_chance_values(self):
        payload = {"elements": [
            {"id": 9, "web_name": "Fernandes", "first_name": "Bruno",
             "second_name": "Fernandes", "news": "",
             "chance_of_playing_next_round": "50",
             "chance_of_playing_this_round": "50"}
        ]}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        items = self._connector(handler=handler).fetch()
        assert len(items) == 1
        assert "chance_of_playing_next_round=50%" in items[0].content_text
# ---------------------------------------------------------------------------
# ConnectorScheduler
# ---------------------------------------------------------------------------


def _make_raw(source_id: str, text: str, external_id: str) -> RawItem:
    return RawItem.create(
        source_id=source_id,
        title=f"item {external_id}",
        content_text=text,
        published_at=NOW,
        scraped_at=NOW,
        ingested_at=NOW,
        external_id=external_id,
    )


class _MockConnector(SourceConnector):
    name = "mock"
    source_id = "mock"

    def __init__(
        self,
        items: list[RawItem] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        super().__init__(
            clock=lambda: NOW,
            monotonic_clock=lambda: 0.0,
            sleep=_noop_sleep,
        )
        self._items = items or []
        self._error = error
        self.fetch_calls = 0

    def fetch(self, *, limit: int | None = None) -> list[RawItem]:
        self.fetch_calls += 1
        if self._error is not None:
            raise self._error
        return list(self._items)


class _FailingItemSink:
    def __init__(self) -> None:
        self.calls: list[RawItem] = []

    def __call__(self, raw: RawItem, *, connector: SourceConnector, dry_run: bool) -> Any:
        self.calls.append(raw)
        if raw.external_id == "bad":
            raise RuntimeError("persist failed")
        return {"ok": True, "id": raw.external_id}


class TestConnectorScheduler:
    def test_run_forwards_items_to_sink_for_all_connectors(self):
        a = _MockConnector([_make_raw("a", "news a1", "1"), _make_raw("a", "news a2", "2")])
        b = _MockConnector([_make_raw("b", "news b1", "3")])
        a.name, b.name = "a", "b"

        seen: list[tuple[str, str]] = []

        def sink(raw, *, connector, dry_run):
            seen.append((connector.name, raw.external_id))

        scheduler = ConnectorScheduler({"a": a, "b": b}, sink)
        report = scheduler.run()
        assert report.total_fetched == 3
        assert report.total_ingested == 3
        assert report.connectors_ran == 2
        assert seen == [("a", "1"), ("a", "2"), ("b", "3")]

    def test_run_single_connector_only(self):
        a = _MockConnector([_make_raw("a", "x", "1")])
        b = _MockConnector([_make_raw("b", "y", "2")])
        a.name, b.name = "a", "b"

        seen: list[str] = []

        def sink(raw, *, connector, dry_run):
            seen.append(connector.name)

        scheduler = ConnectorScheduler({"a": a, "b": b}, sink)
        report = scheduler.run(connector="a")
        assert seen == ["a"]
        assert report.stats_for("a").fetched == 1
        assert "b" not in report.runs

    def test_run_unknown_connector_raises(self):
        scheduler = ConnectorScheduler({}, _noop_sleep)
        with pytest.raises(KeyError):
            scheduler.run(connector="nope")

    def test_run_isolates_fetch_error_per_connector(self):
        ok = _MockConnector([_make_raw("ok", "x", "1")])
        fail = _MockConnector(error=SourceConnectionError("down"))
        ok.name, fail.name = "ok", "fail"

        seen: list[str] = []

        def sink(raw, *, connector, dry_run):
            seen.append(connector.name)

        scheduler = ConnectorScheduler({"ok": ok, "fail": fail}, sink)
        report = scheduler.run()
        assert seen == ["ok"]
        assert report.stats_for("ok").succeeded
        assert not report.stats_for("fail").succeeded
        assert "down" in report.stats_for("fail").errors[0]
        assert not report.succeeded

    def test_run_isolates_sink_error_per_item(self):
        conn = _MockConnector(
            [_make_raw("c", "good", "1"), _make_raw("c", "bad", "bad"),
             _make_raw("c", "good2", "2")]
        )
        sink = _FailingItemSink()
        scheduler = ConnectorScheduler({"c": conn}, sink)
        report = scheduler.run()
        # The 'bad' item failed persistence but '2' still succeeded.
        assert report.stats_for("c").ingested == 2
        assert len(report.stats_for("c").errors) == 1
        assert len(sink.calls) == 3
        assert not report.succeeded

    def test_run_scheduled_runs_expected_number_of_passes(self):
        conn = _MockConnector([_make_raw("c", "x", "1")])
        sleeps: list[float] = []
        scheduler = ConnectorScheduler(
            {"c": conn},
            lambda raw, *, connector, dry_run: None,
            sleep=lambda s: sleeps.append(s),
        )
        reports = scheduler.run_scheduled(interval_seconds=2.0, iterations=3)
        assert len(reports) == 3
        assert conn.fetch_calls == 3
        assert len(sleeps) == 2

    def test_run_scheduled_honours_dry_run_flag(self):
        conn = _MockConnector([_make_raw("c", "x", "1")])
        seen: list[bool] = []

        def sink(raw, *, connector, dry_run):
            seen.append(dry_run)

        scheduler = ConnectorScheduler({"c": conn}, sink, sleep=_noop_sleep)
        reports = scheduler.run_scheduled(
            interval_seconds=0.0, iterations=1, dry_run=True
        )
        assert len(reports) == 1
        assert seen == [True]

    def test_report_to_dict_totals(self):
        conn = _MockConnector([_make_raw("c", "x", "1")])
        scheduler = ConnectorScheduler(
            {"c": conn}, lambda raw, *, connector, dry_run: None
        )
        report = scheduler.run()
        d = report.to_dict()
        assert d["total_fetched"] == 1
        assert d["total_ingested"] == 1
        assert d["succeeded"] is True
        assert "c" in d["connectors"]

    def test_stats_for_creates_zeroed_entry(self):
        report = ConnectorScheduler({}, lambda raw, *, connector, dry_run: None).run()
        stats = report.stats_for("empty")
        assert stats.fetched == 0
        assert stats.ingested == 0


# ---------------------------------------------------------------------------
# Base abstraction
# ---------------------------------------------------------------------------


class TestSourceConnectorBase:
    def test_base_connector_is_abstract(self):
        with pytest.raises(TypeError):
            SourceConnector()

    def test_to_dict_describes_connector(self):
        conn = _MockConnector()
        d = conn.to_dict()
        assert d["name"] == "mock"
        assert d["source_type"]