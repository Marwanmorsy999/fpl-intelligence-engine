"""Phase 9.7 — Live End-to-End Verification.

Manual verification layer for the live ingestion pipeline. It runs the real
RSS feed / official FPL API connectors, pushes the fetched items through the
Phase 9.2 ``ingest_raw_text`` pipeline, and — for the end-to-end verifier —
through extraction, entity resolution, report synthesis and alerting.

* :class:`~fpl_intelligence.live_intelligence.verification.live_verification.RSSFeedVerifier`
  — verifies one live RSS feed (accessibility, parse, Phase 9.2 ingestion).
* :class:`~fpl_intelligence.live_intelligence.verification.live_verification.FPLAPIVerifier`
  — verifies the official FPL ``bootstrap-static`` API the same way.
* :class:`~fpl_intelligence.live_intelligence.verification.live_verification.EndToEndVerifier`
  — verifies the full pipeline: fetch → ingest → extract → resolve → synthesize
  → report → alert → notify.

CLI entry points live in ``scripts/verify_live_rss.py``,
``scripts/verify_live_fpl_api.py`` and ``scripts/verify_live_end_to_end.py``.

This layer is additive: it does **not** modify the quantitative Phases 1–8
stack, it makes **no** live API calls inside ``pytest`` (connectors inject
``httpx.MockTransport``), it hardcodes no API keys, and it performs no
aggressive scraping.
"""
from __future__ import annotations

from fpl_intelligence.live_intelligence.verification.live_verification import (
    DEFAULT_FPL_BOOTSTRAP_URL,
    DEFAULT_MOCK_PLAYER_NAMES,
    DEFAULT_RSS_FEED_URL,
    EndToEndVerification,
    EndToEndVerifier,
    FPLAPIVerifier,
    LiveSourceVerification,
    RSSFeedVerifier,
    VerificationReport,
    VerificationStatus,
    VerificationStep,
    build_verification_session,
)

__all__ = [
    "DEFAULT_FPL_BOOTSTRAP_URL",
    "DEFAULT_MOCK_PLAYER_NAMES",
    "DEFAULT_RSS_FEED_URL",
    "EndToEndVerification",
    "EndToEndVerifier",
    "FPLAPIVerifier",
    "LiveSourceVerification",
    "RSSFeedVerifier",
    "VerificationReport",
    "VerificationStatus",
    "VerificationStep",
    "build_verification_session",
]
