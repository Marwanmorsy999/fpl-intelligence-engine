"""Phase 20.0 — BBC Sport football RSS news radar (free, no key).

Fetches ``feeds.bbci.co.uk/sport/football/rss.xml`` server-side, parses the
RSS items with the stdlib XML parser, and matches headlines against a set of
player names using generated aliases (full name, web name, surname, and the
compact "B.Fernandes" style FPL managers type).

Matching is deliberately conservative: every headline word must appear in the
alias token set for surname-style matches, and keyword hits are limited to
availability vocabulary so random mentions do not raise flags.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

#: BBC Sport football RSS feed (public, no key).
BBC_SPORT_FOOTBALL_RSS = "https://feeds.bbci.co.uk/sport/football/rss.xml"

#: Availability vocabulary that qualifies a matched headline as a flag.
NEWS_KEYWORDS: tuple[str, ...] = (
    "injury", "injured", "injure", "doubt", "out", "ruled out", "sidelined",
    "suspended", "suspension", "ban", "banned", "return", "returns",
    "training", "fitness", "fit again", "knock", "blow", "recovering",
    "recovery", "assessment", "scan", "surgery", "illness",
)

_WORD_RE = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class NewsItem:
    """One parsed RSS headline."""

    title: str
    link: str
    published: str  # ISO-8601 or raw string when unparseable


@dataclass
class PlayerNewsFlag:
    """A matched availability flag for one player."""

    player_id: int
    headline: str
    time: str
    url: str
    matched_alias: str = ""
    keywords_hit: list[str] = field(default_factory=list)


def parse_feed(xml_text: str) -> list[NewsItem]:
    """Parse RSS 2.0 items (title/link/pubDate) from feed text."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[NewsItem] = []
    # Handle both bare <rss> and rdf-flavoured feeds.
    for item in root.iter():
        if not item.tag.endswith("item"):
            continue
        title = link = pub = ""
        for child in item:
            tag = child.tag.rsplit("}", 1)[-1]
            text = (child.text or "").strip()
            if tag == "title" and text:
                title = text
            elif tag == "link" and text:
                link = text
            elif tag == "pubDate" and text:
                pub = _normalise_pub_date(text)
        if title:
            items.append(NewsItem(title=title, link=link, published=pub))
    return items


def _normalise_pub_date(raw: str) -> str:
    """RFC-822 pubDate -> ISO-8601 UTC; raw string as an honest fallback."""
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    except Exception:  # noqa: BLE001 — display-only fallback
        return raw


async def fetch_items(
    url: str = BBC_SPORT_FOOTBALL_RSS,
    timeout: float = 8.0,
) -> list[NewsItem]:
    """Server-side fetch + parse of the RSS feed."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "FPL-Intelligence-Engine/2.0"})
        resp.raise_for_status()
    return parse_feed(resp.text)


# --------------------------------------------------------------------------- #
# Alias generation + matching
# --------------------------------------------------------------------------- #


def _fold(text: str) -> str:
    """Accent-free lowercase key ("Raya Martín" -> "raya martin")."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def build_aliases(web_name: str, first_name: str = "", second_name: str = "") -> set[str]:
    """Generate the match aliases for one player.

    Includes the full name, web name, surname alone, and the compact
    "B.Fernandes"-style initial+surname form managers actually type. Phase 22
    (D5): every alias is accent-folded, and multi-token surnames gain their
    last-token form ("B.Fernandes" -> "fernandes") so headline-only surname
    mentions still match.
    """
    aliases: set[str] = set()
    web = (web_name or "").strip()
    first = (first_name or "").strip()
    second = (second_name or "").strip()

    def add(candidate: str) -> None:
        cleaned = _fold(candidate).strip()
        if len(cleaned) >= 3:
            aliases.add(cleaned)

    add(web)
    add(second)
    if first and second:
        add(f"{first} {second}")
        initial = first[0]
        add(f"{initial}. {second}")
        add(f"{initial}.{second}")
    # D5: "B.Fernandes" as a WEB NAME folds to tokens ["b", "fernandes"];
    # expose the bare surname token so "Fernandes" headlines still match.
    web_tokens = [t for t in re.split(r"[\s\-.']+", _fold(web)) if t]
    if len(web_tokens) > 1:
        add(web_tokens[-1])
    if second:
        surname_tokens = [t for t in re.split(r"[\s\-']+", _fold(second)) if t]
        if len(surname_tokens) > 1:
            add(surname_tokens[-1])
    return aliases


def _alias_matches(alias: str, headline_lower: str) -> bool:
    """Alias-in-headline test with word boundaries; dots act as separators."""
    pattern = r"(?<![a-z0-9])" + re.escape(alias).replace(r"\.", r"\.?\s*") + r"(?![a-z0-9])"
    return re.search(pattern, headline_lower) is not None


def _published_key(published: str) -> float:
    """Sortable recency key; unparseable strings sort oldest instead of winning."""
    try:
        parsed = datetime.fromisoformat((published or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except ValueError:
        return float("-inf")


def match_headlines(
    items: Sequence[NewsItem],
    players: Sequence[tuple[int, str, str, str]],
    keywords: Sequence[str] = NEWS_KEYWORDS,
) -> dict[str, dict[str, Any]]:
    """Match feed items against players.

    ``players`` is ``(player_id, web_name, first_name, second_name)`` tuples.
    Returns ``{str(player_id): {headline, time, url, alias}}`` keeping only the
    most recent matching headline per player. Players with no match are absent.
    Phase 22 (D5): alias and headline comparison are accent-folded, and
    recency compares parsed timestamps rather than raw strings.
    """
    flags: dict[str, dict[str, Any]] = {}
    kw_lower = [_fold(k) for k in keywords]
    prepared: list[tuple[float, NewsItem]] = [
        (_published_key(item.published), item) for item in items
    ]
    for pid, web_name, first_name, second_name in players:
        best: NewsItem | None = None
        best_alias = ""
        best_kws: list[str] = []
        for _published_ts, item in sorted(prepared, key=lambda pair: pair[0], reverse=True):
            lower = _fold(item.title)
            hit_keywords = [k for k in kw_lower if k and k in lower]
            if not hit_keywords:
                continue
            for alias in build_aliases(web_name, first_name, second_name):
                if _alias_matches(alias, lower):
                    best = item
                    best_alias = alias
                    best_kws = hit_keywords[:3]
                    break
            if best is not None:
                break  # items were scanned newest-first; first hit wins
        if best is not None:
            flags[str(pid)] = {
                "headline": best.title,
                "time": best.published,
                "url": best.link,
                "matched_alias": best_alias,
                "keywords": best_kws,
            }
    return flags


def scan_now(
    items: Sequence[NewsItem],
    players: Sequence[tuple[int, str, str, str]],
) -> dict[str, Any]:
    """Bundle a scan result with honest metadata."""
    flags = match_headlines(items, players)
    return {
        "scanned_at": datetime.now(UTC).isoformat(),
        "headlines_scanned": len(items),
        "matches_found": len(flags),
        "news_flags": flags,
    }
