"""Phase 20.0 — BBC news radar: RSS parsing, aliases, headline matching."""

from __future__ import annotations

from fpl_intelligence.data_providers.bbc_news import (
    NewsItem,
    build_aliases,
    match_headlines,
    parse_feed,
)

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:media="http://search.yahoo.com/mrss/" version="2.0">
  <channel>
    <title>BBC Sport - Football</title>
    <item>
      <title>Bruno Fernandes returns to full training after injury</title>
      <link>https://www.bbc.co.uk/sport/football/1</link>
      <pubDate>Fri, 21 Aug 2026 09:15:00 GMT</pubDate>
    </item>
    <item>
      <title>Saliba a major doubt for the weekend with a knock</title>
      <link>https://www.bbc.co.uk/sport/football/2</link>
      <pubDate>Thu, 20 Aug 2026 18:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Transfer deadline day: five deals that could still happen</title>
      <link>https://www.bbc.co.uk/sport/football/3</link>
      <pubDate>Thu, 20 Aug 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


class TestParseFeed:
    def test_extracts_items(self):
        items = parse_feed(RSS_SAMPLE)
        assert len(items) == 3
        assert items[0].title.startswith("Bruno Fernandes returns")
        assert items[0].link.endswith("/1")
        # RFC-822 -> ISO-8601
        assert items[0].published.startswith("2026-08-21")

    def test_invalid_xml_returns_empty_not_crash(self):
        assert parse_feed("<not-xml") == []

    def test_items_without_title_are_skipped(self):
        feed = "<rss><channel><item><link>x</link></item></channel></rss>"
        assert parse_feed(feed) == []


class TestAliases:
    def test_generates_initial_surname_style(self):
        aliases = build_aliases("B.Fernandes", "Bruno", "Fernandes")
        assert "b.fernandes" in aliases or "b. fernandes" in aliases
        assert "bruno fernandes" in aliases
        assert "fernandes" in aliases

    def test_minimum_length_guard(self):
        # Two-letter surnames alone must not become aliases (too noisy).
        aliases = build_aliases("Al", "Wesley", "Al")
        assert "al" not in aliases


class TestMatchHeadlines:
    ITEMS = parse_feed(RSS_SAMPLE)

    PLAYERS = [
        (411, "B.Fernandes", "Bruno", "Fernandes"),
        (425, "Saliba", "William", "Saliba"),
        (300, "Unrelated", "Nobody", "Related"),
    ]

    def test_matches_availability_headlines_only(self):
        flags = match_headlines(self.ITEMS, self.PLAYERS)
        # The transfer-rumour item mentions no squad player AND no keyword.
        assert "300" not in flags
        assert set(flags) == {"411", "425"}

    def test_flag_payload_shape(self):
        flags = match_headlines(self.ITEMS, self.PLAYERS)
        flag = flags["411"]
        assert set(flag) == {"headline", "time", "url", "matched_alias", "keywords"}
        assert "Bruno Fernandes" in flag["headline"]
        assert flag["url"].endswith("/1")
        assert any(k in ("return", "training") for k in flag["keywords"])

    def test_compact_alias_hits_headline(self):
        # "B.Fernandes" alias should match the "Bruno Fernandes" headline.
        flags = match_headlines(self.ITEMS, self.PLAYERS)
        assert flags["411"]["matched_alias"] in ("b.fernandes", "b. fernandes",
                                                 "bruno fernandes", "fernandes")

    def test_non_matching_players_absent(self):
        flags = match_headlines(self.ITEMS, self.PLAYERS)
        assert "300" not in flags

    def test_no_items_means_no_flags(self):
        assert match_headlines([], self.PLAYERS) == {}

    def test_word_boundary_prevents_substring_hits(self):
        items = [NewsItem(title="Antonee out for season", link="x", published="")]
        players = [(1, "One", "Tony", "One")]  # "one" is inside "antonee"
        assert match_headlines(items, players, keywords=("out",)) == {}
