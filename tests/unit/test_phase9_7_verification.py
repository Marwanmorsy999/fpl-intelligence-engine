"""Phase 9.7 unit tests — Live End-to-End Verification.

Covers the three Phase 9.7 verifiers with **mocked HTTP responses**
(``httpx.MockTransport``) and an in-memory SQLite database, so no live network
call is ever made inside ``pytest``:

* :class:`RSSFeedVerifier` — accessibility, parse, Phase 9.2 ingestion;
* :class:`FPLAPIVerifier` — the same for the official FPL API payload;
* :class:`EndToEndVerifier` — the full pipeline: fetch -> ingest -> extract ->
  resolve -> synthesize -> report -> alert -> notify.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import Gameweek, Player, Season
from fpl_intelligence.live_intelligence.connectors import (
    FPLAPIConnector,
    RSSConnector,
)
from fpl_intelligence.live_intelligence.mock_llm import make_mock_provider
from fpl_intelligence.live_intelligence.verification import (
    EndToEndVerifier,
    FPLAPIVerifier,
    RSSFeedVerifier,
)

NOW = datetime(2025, 8, 16, 12, 0, 0, tzinfo=UTC)

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Salah injury doubt</title>
      <description>Mohamed Salah missed training and is a doubt for the next
        match with a hamstring injury.</description>
      <link>https://example.com/salah</link>
      <guid>guid-salah</guid>
      <pubDate>Thu, 14 Aug 2025 09:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Haaland fitness update</title>
      <description>Erling Haaland back in full training ahead of the weekend.</description>
      <link>https://example.com/haaland</link>
      <guid>guid-haaland</guid>
      <pubDate>Thu, 14 Aug 2025 09:05:00 GMT</pubDate>
    </item>
    <item>
      <title>No content item</title>
      <link>https://example.com/empty</link>
      <pubDate>Thu, 14 Aug 2025 09:10:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

FPL_PAYLOAD = {
    "elements": [
        {
            "id": 1,
            "web_name": "Salah",
            "first_name": "Mohamed",
            "second_name": "Salah",
            "news": "Hamstring injury — doubt for the next round",
            "chance_of_playing_next_round": 50,
            "chance_of_playing_this_round": 75,
        },
        {
            "id": 2,
            "web_name": "Haaland",
            "first_name": "Erling",
            "second_name": "Haaland",
            "news": "",
            "chance_of_playing_next_round": 50,
            "chance_of_playing_this_round": 100,
        },
        {
            "id": 3,
            "web_name": "Son",
            "first_name": "Heung-min",
            "second_name": "Son",
            "news": "",
            "chance_of_playing_next_round": 100,
            "chance_of_playing_this_round": 100,
        },
    ]
}


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Build an httpx.Client whose transport is entirely mocked (no network)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_rss() -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=RSS_XML)

    return handler


def _ok_fpl() -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=FPL_PAYLOAD)

    return handler


def _down(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(500)


def _make_rss_connector(
    handler: Callable[[httpx.Request], httpx.Response],
) -> RSSConnector:
    return RSSConnector(
        "https://example.com/feed.rss",
        source_id="rss_feed",
        http_client=_make_client(handler),
        clock=lambda: NOW,
    )


def _make_fpl_connector(
    handler: Callable[[httpx.Request], httpx.Response],
) -> FPLAPIConnector:
    return FPLAPIConnector(
        api_url="https://example.com/bootstrap-static/",
        http_client=_make_client(handler),
        clock=lambda: NOW,
    )


@pytest.fixture
def db_session_factory() -> Any:
    """A session factory over a shared in-memory SQLite database."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def seeded_session_factory(db_session_factory: Any) -> Any:
    """A factory pre-seeded with a resolvable player + a future Gameweek."""
    db: Session = db_session_factory()
    season = Season(code="2025-26", display_name="2025/26")
    db.add(season)
    db.flush()
    db.add(
        Gameweek(
            season_id=season.id,
            provider_event_id=3,
            name="Gameweek 3",
            deadline_time=datetime.now(UTC) + timedelta(days=7),
            status="scheduled",
        )
    )
    db.add(Player(first_name="Mohamed", second_name="Salah", web_name="Salah"))
    db.commit()
    db.close()
    return db_session_factory


# ---------------------------------------------------------------------------
# RSSFeedVerifier
# ---------------------------------------------------------------------------


class TestRSSFeedVerifier:
    def test_accessible_feed_parses_and_ingests(self, db_session_factory: Any) -> None:
        verifier = RSSFeedVerifier(
            connector=_make_rss_connector(_ok_rss()),
            session_factory=db_session_factory,
            llm_provider=make_mock_provider(player_names=["Salah", "Haaland"]),
        )
        report = verifier.verify(limit=10)

        assert report.passed
        assert report.fetched == 2  # the content-less item is dropped by the parser
        assert report.parsed == 2
        assert report.ingested == 2
        assert report.sample_titles == ["Salah injury doubt", "Haaland fitness update"]
        step_names = [step.name for step in report.steps]
        assert step_names == ["connectivity", "parse", "ingest"]
        assert all(step.ok for step in report.steps)

    def test_reports_connectivity_failure(self, db_session_factory: Any) -> None:
        verifier = RSSFeedVerifier(
            connector=_make_rss_connector(_down),
            session_factory=db_session_factory,
        )
        report = verifier.verify()

        assert not report.passed
        assert report.fetched == 0
        assert report.steps[0].name == "connectivity"
        assert not report.steps[0].ok
        assert report.errors

    def test_reports_invalid_xml_parse_failure(self, db_session_factory: Any) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<not><valid></rss>")

        verifier = RSSFeedVerifier(
            connector=_make_rss_connector(handler),
            session_factory=db_session_factory,
        )
        report = verifier.verify()

        assert not report.passed
        assert report.errors
        assert report.ingested == 0

    def test_duplicates_detected_on_second_pass(self, db_session_factory: Any) -> None:
        verifier = RSSFeedVerifier(
            connector=_make_rss_connector(_ok_rss()),
            session_factory=db_session_factory,
        )
        first = verifier.verify(limit=10)
        second = verifier.verify(limit=10)

        assert first.ingested == 2
        assert second.ingested == 0
        assert second.duplicates == 2
        assert second.passed

    def test_dry_run_ingests_in_memory_only(self, db_session_factory: Any) -> None:
        verifier = RSSFeedVerifier(
            connector=_make_rss_connector(_ok_rss()),
            session_factory=db_session_factory,
            llm_provider=make_mock_provider(player_names=["Salah", "Haaland"]),
        )
        report = verifier.verify(limit=10, persist=False)

        # Dry-run still reports the full pipeline as-if, without persisting.
        assert report.passed
        assert report.fetched == 2
        assert report.ingested == 2
        assert report.steps[-1].name == "ingest"
        assert report.steps[-1].ok


# ---------------------------------------------------------------------------
# FPLAPIVerifier
# ---------------------------------------------------------------------------


class TestFPLAPIVerifier:
    def test_live_api_parses_and_ingests(self, db_session_factory: Any) -> None:
        verifier = FPLAPIVerifier(
            connector=_make_fpl_connector(_ok_fpl()),
            session_factory=db_session_factory,
            llm_provider=make_mock_provider(player_names=["Salah", "Haaland"]),
        )
        report = verifier.verify(limit=10)

        assert report.passed
        assert report.fetched == 2  # news + availability risk; the healthy player is filtered
        assert report.parsed == 2
        assert report.ingested == 2
        assert report.source == "fpl_api_official"
        assert report.sample_titles[0] == "Salah"

    def test_api_connection_failure_reported(self, db_session_factory: Any) -> None:
        verifier = FPLAPIVerifier(
            connector=_make_fpl_connector(_down),
            session_factory=db_session_factory,
        )
        report = verifier.verify()

        assert not report.passed
        assert report.fetched == 0
        assert not report.steps[0].ok
        assert "unreachable" in report.steps[0].detail

    def test_invalid_json_parse_failure(self, db_session_factory: Any) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>not json</html>")

        verifier = FPLAPIVerifier(
            connector=_make_fpl_connector(handler),
            session_factory=db_session_factory,
        )
        report = verifier.verify()

        assert not report.passed
        assert report.errors
        assert report.ingested == 0

    def test_fully_available_players_produce_no_items(self, db_session_factory: Any) -> None:
        payload = {
            "elements": [
                {
                    "id": 9,
                    "web_name": "Healthy",
                    "first_name": "Fit",
                    "second_name": "Player",
                    "news": "",
                    "chance_of_playing_next_round": 100,
                    "chance_of_playing_this_round": 100,
                }
            ]
        }

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        verifier = FPLAPIVerifier(
            connector=_make_fpl_connector(handler),
            session_factory=db_session_factory,
        )
        report = verifier.verify()

        # No availability signal, so no ingestible item — but the API itself is fine.
        assert report.fetched == 0
        assert report.ingested == 0
        assert report.passed


# ---------------------------------------------------------------------------
# EndToEndVerifier
# ---------------------------------------------------------------------------


class TestEndToEndVerifier:
    def _verifier(self, session_factory: Any, **kwargs: Any) -> EndToEndVerifier:
        connectors: dict[str, Any] = {
            "rss": _make_rss_connector(_ok_rss()),
            "fpl_api": _make_fpl_connector(_ok_fpl()),
        }
        return EndToEndVerifier(
            connectors=connectors,
            session_factory=session_factory,
            llm_provider=make_mock_provider(player_names=["Salah", "Haaland"]),
            gameweek=3,
            season_code="2025-26",
            gameweek_number=3,
            **kwargs,
        )

    def test_full_pipeline_all_stages_pass(self, seeded_session_factory: Any) -> None:
        verifier = self._verifier(seeded_session_factory)
        report = verifier.verify(limit=10)

        assert report.passed
        assert report.total_fetched == 4
        assert report.total_ingested == 4
        assert report.extraction_runs == 4
        assert report.availability_evidence >= 1
        assert report.resolved_entities >= 1  # 'Salah' resolves against the seeded Player
        assert report.reports_generated == 1
        assert report.report_citations >= 1
        assert report.player_id is not None

        step_names = [step.name for step in report.steps]
        assert step_names == [
            "fetch",
            "ingest",
            "extract",
            "resolve",
            "synthesize",
            "report",
            "alert",
        ]
        assert all(step.ok for step in report.steps)

    def test_report_synthesised_with_quantitative_baseline(
        self, seeded_session_factory: Any
    ) -> None:
        verifier = self._verifier(seeded_session_factory)
        report = verifier.verify(limit=10)

        report_step = next(step for step in report.steps if step.name == "report")
        assert report_step.ok
        assert "Quantitative Baseline" in report_step.detail

    def test_alerts_generated_and_delivered(self, seeded_session_factory: Any) -> None:
        verifier = self._verifier(seeded_session_factory)
        report = verifier.verify(limit=10)

        # Injury / availability-risk items must surface alerts, and every alert
        # must reach the recording notifier.
        assert report.alerts >= 2
        assert report.notifications_delivered == report.alerts

    def test_fetch_failure_reported_when_sources_down(
        self, db_session_factory: Any
    ) -> None:
        connectors = {
            "rss": _make_rss_connector(_down),
            "fpl_api": _make_fpl_connector(_down),
        }
        verifier = EndToEndVerifier(
            connectors=connectors,
            session_factory=db_session_factory,
        )
        report = verifier.verify()

        assert not report.passed
        assert report.total_fetched == 0
        fetch_step = next(step for step in report.steps if step.name == "fetch")
        assert not fetch_step.ok
        assert report.errors

    def test_rss_only_connector_run(self, db_session_factory: Any) -> None:
        verifier = EndToEndVerifier(
            connectors={"rss": _make_rss_connector(_ok_rss())},
            session_factory=db_session_factory,
            llm_provider=make_mock_provider(player_names=["Salah", "Haaland"]),
        )
        report = verifier.verify(limit=10)

        assert report.passed
        assert report.connector_fetched == {"rss": 2}
        assert report.connector_ingested == {"rss": 2}

    def test_dry_run_runs_stages_without_report(self, db_session_factory: Any) -> None:
        verifier = self._verifier(db_session_factory)
        report = verifier.verify(limit=10, persist=False)

        assert report.dry_run is True
        assert report.passed
        assert report.total_fetched == 4
        assert report.extraction_runs == 4
        assert report.alerts >= 2
        # Dry-run rolls the evidence back, so report synthesis is skipped.
        assert report.reports_generated == 0

    def test_report_dict_round_trip(self, db_session_factory: Any) -> None:
        verifier = self._verifier(db_session_factory)
        report = verifier.verify(limit=5)

        data = report.to_dict()
        assert data["layer"] == "live_end_to_end"
        assert data["passed"] is True
        assert data["total_fetched"] == 4
        assert data["dry_run"] is False
        assert {step["name"] for step in data["steps"]} == {
            "fetch",
            "ingest",
            "extract",
            "resolve",
            "synthesize",
            "report",
            "alert",
        }



