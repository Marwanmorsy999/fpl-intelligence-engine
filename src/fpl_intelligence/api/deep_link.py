"""Phase 5 — deep-link URL helpers.

The dashboard exposes a single canonical deep-link shape that can be
copied, bookmarked, and shared:

    https://fpl-intelligence-engine-foundation.vercel.app/?entry=2295006

This module is the single source of truth for parsing and building
deep-links so every endpoint that takes an ``entry`` query parameter
behaves the same way.

Design rules
------------

* The only query parameter we honor on the dashboard is ``entry``.
  Anything else is ignored.
* The entry value must be a numeric string of 1-8 digits (FPL entry
  ids are short). Non-numeric values raise :class:`InvalidDeepLink`.
* Empty / missing ``entry`` is valid: the dashboard falls back to
  demo mode.

Helpers
-------

- :func:`parse_entry` — pull ``entry`` from a query-string mapping.
- :func:`build_deep_link` — construct the canonical URL for an entry.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


class InvalidDeepLink(ValueError):
    """Raised when an entry id is malformed (non-numeric, too long, ...)."""


_MAX_ENTRY_LENGTH = 8
_DEMO_ENTRY = "demo"


def parse_entry(query_string: str) -> str | None:
    """Extract the canonical ``entry`` value from a query string.

    Returns the entry id as a string (so the caller can decide how to
    coerce it), or ``None`` for demo / missing.
    """
    if not query_string:
        return None
    parsed = parse_qs(query_string, keep_blank_values=False)
    raw = parsed.get("entry", [None])[0]
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    return _normalise(raw)


def _normalise(value: str) -> str:
    if value == _DEMO_ENTRY:
        return value
    if not value.isdigit():
        raise InvalidDeepLink(f"entry must be numeric or 'demo': {value!r}")
    if len(value) > _MAX_ENTRY_LENGTH:
        raise InvalidDeepLink(f"entry too long: {value!r}")
    return value


def build_deep_link(base_url: str, entry: str | int | None) -> str:
    """Build a canonical deep-link URL for an FPL entry.

    ``base_url`` is the dashboard origin (scheme + host). When
    ``entry`` is None or the demo sentinel, no query string is
    appended and the caller can decide what to render.
    """
    parsed = urlparse(base_url)
    if entry is None or entry == _DEMO_ENTRY:
        return urlunparse(parsed._replace(query="", fragment=""))
    normalised = _normalise(str(entry).strip())
    return urlunparse(
        parsed._replace(
            query=urlencode({"entry": normalised}),
            fragment="",
        )
    )
