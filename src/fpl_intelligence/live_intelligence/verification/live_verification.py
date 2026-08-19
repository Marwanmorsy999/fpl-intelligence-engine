"""Phase 9.7 — Live End-to-End Verification.

The live verification layer runs the ingestion pipeline against real sources
(a BBC RSS team feed and the official FPL ``bootstrap-static`` API) and reports,
per stage, whether the system actually works end-to-end:

1. **fetch** — the Phase 9.5 connectors can reach the source (accessibility + parse);
2. **ingest** — the fetched items flow into the Phase 9.2 ``ingest_raw_text`` pipeline;
3. **extract** — an LLM extraction run is produced for every ingested item;
4. **resolve** — entities are resolved and unresolved/ambiguous evidence is
   recorded and handled (``unresolved_live_evidence``), never silently dropped;
5. **synthesize** — the Phase 9.4 :class:`AnalystReportGenerator` merges the
   evidence with quantitative predictions into an ``IntelligenceReport``;
6. **alert / notify** — the Phase 9.6 ``AlertGenerator`` turns the items into
   alerts and the ``NotificationService`` delivers them to a notifier channel.

Three verifiers ship:

* :class:`RSSFeedVerifier` — one real RSS feed (requirement 1);
* :class:`FPLAPIVerifier` — the official FPL API (requirement 2);
* :class:`EndToEndVerifier` — the full pipeline over injected connectors
  (requirement 3).

Design rules (mirroring the rest of Phase 9):

* **Offline-testable.** Connectors accept an injected ``httpx.Client``
  (tests use ``httpx.MockTransport``), and the default LLM provider is the
  deterministic :class:`MockLLMProvider`, so ``pytest`` never touches the
  network and never spends quota.
* **Additive.** This layer does not modify the quantitative Phases 1–8 stack:
  it consumes the existing connectors, the Phase 9.2 pipeline, the Phase 9.4
  bridge and the Phase 9.6 scheduler read-only.
* **No hardcoded keys.** Credentials are only ever supplied by the caller
  (CLI arguments / environment variables).
* **No aggressive scraping.** Only rate-limited RSS polling and the official
  FPL API endpoint are used.

``persist=True`` commits the ingestion rows into the verification database
(an in-memory SQLite by default, so a live run never writes to a real DB
unless ``--db`` is given). ``persist=False`` (``--dry-run``) still runs the
entire pipeline but rolls back, which is the safe live-smoke-test mode.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.availability.models import AvailabilityEvidence
from fpl_intelligence.db.base import Base
from fpl_intelligence.live_intelligence.analyst import AnalystTask
from fpl_intelligence.live_intelligence.bridge import (
    AnalystReportGenerator,
    EvidenceQueryService,
    PredictionContextBuilder,
    StaticPredictionProvider,
)
from fpl_intelligence.live_intelligence.connectors import (
    FPL_BOOTSTRAP_URL,
    FPLAPIConnector,
    RSSConnector,
    SourceConnector,
    SourceConnectorError,
)
from fpl_intelligence.live_intelligence.extraction import LLMProvider
from fpl_intelligence.live_intelligence.mock_llm import make_mock_provider
from fpl_intelligence.live_intelligence.models import TacticalEvidence
from fpl_intelligence.live_intelligence.raw_item_ledger import (
    ManualIngestReport,
    ManualIngestStatus,
    RawItem,
    ingest_raw_text,
)
from fpl_intelligence.live_intelligence.scheduling import (
    AlertGenerator,
    NotificationService,
    RecordingNotifier,
    Scheduler,
)
from fpl_intelligence.live_intelligence.temporal_ledger import utc_now
from fpl_intelligence.optimization.provider import DecisionPredictionProvider

#: Default RSS feed used by the live scripts (public BBC sport team feed).
DEFAULT_RSS_FEED_URL = "https://www.bbc.co.uk/sport/football/teams/rss"
#: Official FPL public "static bootstrap" endpoint (no key required).
DEFAULT_FPL_BOOTSTRAP_URL = FPL_BOOTSTRAP_URL

#: Player names the default MockLLMProvider is willing to attribute evidence
#: to, so a live (offline) run still extracts attributable evidence when a
#: feed mentions a well-known name. A real provider ignores this list.
DEFAULT_MOCK_PLAYER_NAMES = (
    "Salah",
    "Haaland",
    "Son",
    "Saka",
    "Foden",
    "Palmer",
    "Isak",
)


class VerificationStatus(StrEnum):
    """Outcome of one verification stage."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class VerificationStep:
    """One named stage of a verification run."""

    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": (
                VerificationStatus.PASS.value
                if self.ok
                else VerificationStatus.FAIL.value
            ),
            "ok": self.ok,
            "detail": self.detail,
        }


@dataclass
class VerificationReport:
    """Base result shape shared by every verifier."""

    layer: str
    steps: list[VerificationStep] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when every stage passed and no error was recorded."""
        return all(step.ok for step in self.steps) and not self.errors

    def add_step(self, name: str, ok: bool, detail: str = "") -> None:
        """Append one stage result."""
        self.steps.append(VerificationStep(name=name, ok=ok, detail=detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "passed": self.passed,
            "steps": [step.to_dict() for step in self.steps],
            "errors": list(self.errors),
        }


@dataclass
class LiveSourceVerification(VerificationReport):
    """Result of verifying one live source (RSS feed or FPL API)."""

    source: str = ""
    fetched: int = 0
    parsed: int = 0
    ingested: int = 0
    duplicates: int = 0
    sample_titles: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result.update(
            {
                "source": self.source,
                "fetched": self.fetched,
                "parsed": self.parsed,
                "ingested": self.ingested,
                "duplicates": self.duplicates,
                "sample_titles": list(self.sample_titles),
            }
        )
        return result


@dataclass
class EndToEndVerification(VerificationReport):
    """Result of the full live pipeline verification."""

    connector_fetched: dict[str, int] = field(default_factory=dict)
    connector_ingested: dict[str, int] = field(default_factory=dict)
    total_fetched: int = 0
    total_ingested: int = 0
    extraction_runs: int = 0
    availability_evidence: int = 0
    tactical_evidence: int = 0
    resolved_entities: int = 0
    unresolved_evidence: int = 0
    ambiguous_entities: int = 0
    player_id: int | None = None
    reports_generated: int = 0
    report_citations: int = 0
    alerts: int = 0
    notifications_delivered: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result.update(
            {
                "connector_fetched": dict(self.connector_fetched),
                "connector_ingested": dict(self.connector_ingested),
                "total_fetched": self.total_fetched,
                "total_ingested": self.total_ingested,
                "extraction_runs": self.extraction_runs,
                "availability_evidence": self.availability_evidence,
                "tactical_evidence": self.tactical_evidence,
                "resolved_entities": self.resolved_entities,
                "unresolved_evidence": self.unresolved_evidence,
                "ambiguous_entities": self.ambiguous_entities,
                "player_id": self.player_id,
                "reports_generated": self.reports_generated,
                "report_citations": self.report_citations,
                "alerts": self.alerts,
                "notifications_delivered": self.notifications_delivered,
                "dry_run": self.dry_run,
            }
        )
        return result


def build_verification_session(db_url: str | None = None) -> Any:
    """Build a ``sessionmaker`` over SQLite for verification runs.

    With no ``db_url`` an **in-memory, shared-connection** SQLite database is
    used (``StaticPool``), so multiple sessions created from the returned
    factory see the same data — the dedupe and post-ingest queries a
    verification performs require this. When ``db_url`` names a file it is a
    plain file-backed engine.
    """
    if db_url is None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False,
        )
    else:
        engine = create_engine(db_url, echo=False)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class FPLAPIVerifier:
    """Verify the official FPL API end-to-end.

    Checks (1) ``bootstrap-static`` is reachable and parses into
    :class:`RawItem` objects, and (2) its items are ingested into the Phase 9.2
    pipeline.

    Args:
        connector: An :class:`FPLAPIConnector`. When omitted one is built from
            ``api_url`` / ``http_client`` (the ``http_client`` seam lets tests
            mock the network).
        session_factory: SQLAlchemy sessionmaker for the verification DB.
        llm_provider: LLM used by ingestion; defaults to :class:`MockLLMProvider`.
    """

    def __init__(
        self,
        connector: FPLAPIConnector | None = None,
        *,
        api_url: str = DEFAULT_FPL_BOOTSTRAP_URL,
        http_client: Any | None = None,
        session_factory: Any | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self._connector = connector or FPLAPIConnector(
            api_url=api_url, http_client=http_client
        )
        self._session_factory = session_factory or build_verification_session()
        self._llm_provider = llm_provider or make_mock_provider()

    @property
    def connector(self) -> FPLAPIConnector:
        """The connector under test (for inspection / cleanup)."""
        return self._connector

    def verify(self, *, limit: int = 20, persist: bool = True) -> LiveSourceVerification:
        """Run accessibility + parse + ingest verification and return a report."""
        report = LiveSourceVerification(
            layer="live_fpl_api", source=self._connector.source_id
        )

        try:
            items = self._connector.fetch(limit=limit)
        except SourceConnectorError as exc:
            report.add_step(
                "connectivity",
                False,
                f"{self._connector.api_url} unreachable: {exc}",
            )
            report.errors.append(str(exc))
            return report
        report.fetched = len(items)
        report.add_step(
            "connectivity",
            True,
            f"GET {self._connector.api_url} -> {report.fetched} item(s)",
        )

        malformed = [
            item.title or "<untitled>"
            for item in items
            if not (item.title and item.content_text and item.published_at)
        ]
        report.parsed = report.fetched - len(malformed)
        report.sample_titles = [item.title for item in items[:5]]
        detail = (
            f"parsed {report.parsed}/{report.fetched} item(s) into RawItem"
            f" (title/content/published_at present)"
        )
        if malformed:
            detail += f"; malformed: {', '.join(malformed)}"
        report.add_step("parse", not malformed, detail)

        db: Session = self._session_factory()
        rejected: list[str] = []
        try:
            for item in items:
                result = ingest_raw_text(
                    db,
                    source_id=item.source_id,
                    text=item.content_text,
                    published_at=item.published_at,
                    url=item.url,
                    title=item.title,
                    external_id=item.external_id,
                    source_type=self._connector.source_type,
                    provider=self._llm_provider,
                    dry_run=not persist,
                )
                if result.status == ManualIngestStatus.CREATED:
                    report.ingested += 1
                elif result.status == ManualIngestStatus.DUPLICATE:
                    report.duplicates += 1
                else:
                    rejected.append(result.error or str(result.status))
        finally:
            db.close()

        if rejected:
            report.errors.extend(rejected)
        report.add_step(
            "ingest",
            not rejected,
            f"{report.ingested} ingested, {report.duplicates} duplicate(s)"
            + (f", {len(rejected)} rejected" if rejected else ""),
        )
        return report


class RSSFeedVerifier:
    """Verify one live RSS feed end-to-end.

    Checks (1) the feed is reachable and parses into :class:`RawItem` objects,
    and (2) its items are ingested into the Phase 9.2 pipeline.

    Args:
        connector: An :class:`RSSConnector`. When omitted one is built from
            ``feed_url`` / ``source_id`` / ``http_client`` (the ``http_client``
            seam is what lets tests mock the network).
        session_factory: SQLAlchemy sessionmaker for the verification DB.
        llm_provider: LLM used by ingestion; defaults to :class:`MockLLMProvider`.
    """

    def __init__(
        self,
        connector: RSSConnector | None = None,
        *,
        feed_url: str = DEFAULT_RSS_FEED_URL,
        source_id: str = "rss_feed",
        http_client: Any | None = None,
        session_factory: Any | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self._connector = connector or RSSConnector(
            feed_url, source_id=source_id, http_client=http_client
        )
        self._session_factory = session_factory or build_verification_session()
        self._llm_provider = llm_provider or make_mock_provider()

    @property
    def connector(self) -> RSSConnector:
        """The connector under test (for inspection / cleanup)."""
        return self._connector

    def verify(self, *, limit: int = 20, persist: bool = True) -> LiveSourceVerification:
        """Run accessibility + parse + ingest verification and return a report."""
        report = LiveSourceVerification(
            layer="live_rss_feed", source=self._connector.source_id
        )

        try:
            items = self._connector.fetch(limit=limit)
        except SourceConnectorError as exc:
            report.add_step(
                "connectivity",
                False,
                f"{self._connector.feed_url} unreachable: {exc}",
            )
            report.errors.append(str(exc))
            return report
        report.fetched = len(items)
        report.add_step(
            "connectivity",
            True,
            f"GET {self._connector.feed_url} -> {report.fetched} item(s)",
        )

        malformed = [
            item.title or "<untitled>"
            for item in items
            if not (item.title and item.content_text and item.published_at)
        ]
        report.parsed = report.fetched - len(malformed)
        report.sample_titles = [item.title for item in items[:5]]
        detail = (
            f"parsed {report.parsed}/{report.fetched} item(s) into RawItem"
            f" (title/content/published_at present)"
        )
        if malformed:
            detail += f"; malformed: {', '.join(malformed)}"
        report.add_step("parse", not malformed, detail)

        db: Session = self._session_factory()
        rejected: list[str] = []
        try:
            for item in items:
                result = ingest_raw_text(
                    db,
                    source_id=item.source_id,
                    text=item.content_text,
                    published_at=item.published_at,
                    url=item.url,
                    title=item.title,
                    external_id=item.external_id,
                    source_type=self._connector.source_type,
                    provider=self._llm_provider,
                    dry_run=not persist,
                )
                if result.status == ManualIngestStatus.CREATED:
                    report.ingested += 1
                elif result.status == ManualIngestStatus.DUPLICATE:
                    report.duplicates += 1
                else:
                    rejected.append(result.error or str(result.status))
        finally:
            db.close()

        if rejected:
            report.errors.extend(rejected)
        report.add_step(
            "ingest",
            not rejected,
            f"{report.ingested} ingested, {report.duplicates} duplicate(s)"
            + (f", {len(rejected)} rejected" if rejected else ""),
        )
        return report
class EndToEndVerifier:
    """Run the full live pipeline and verify every stage.

    One pass over injected connectors drives the Phase 9.6 :class:`Scheduler`
    (fetch -> ingest -> alert -> notify), and the Phase 9.4
    :class:`AnalystReportGenerator` adds the synthesize/report stage on top of
    the freshly-committed evidence. Every HTTP call happens inside a connector,
    so tests inject ``httpx.MockTransport``-backed connectors and the whole
    pipeline is exercised offline.

    Args:
        connectors: Pre-built connector map (name -> :class:`SourceConnector`).
            When omitted, live RSS and FPL connectors are built from the
            ``feed_url`` / ``api_url`` arguments.
        rss_connector / fpl_connector: Convenience single-connector inputs that
            are merged when ``connectors`` is not given.
        session_factory: SQLAlchemy sessionmaker for the verification DB.
        llm_provider: LLM for extraction and report synthesis; defaults to
            :class:`MockLLMProvider` (attributable well-known names).
        prediction_provider: Read-only Phase 4/5/6 provider for the report;
            defaults to the offline :class:`StaticPredictionProvider`.
        task: Analyst task for the synthesis stage.
        player_id: Subject player for the report. When ``None`` the player with
            the most resolved evidence is auto-discovered.
        gameweek: FPL gameweek used by the synthesis stage.
        subject_label: Human-readable report label.
        season_code / gameweek_number: Optional Phase 9.2 deadline context. When
            supplied, ingested items are classified against the matching
            ``Gameweek`` deadline, so the evidence can be synthesised into the
            report (NO_DEADLINE_CONTEXT evidence is excluded by the analyst).
    """

    def __init__(
        self,
        *,
        connectors: Mapping[str, SourceConnector] | None = None,
        rss_connector: RSSConnector | None = None,
        fpl_connector: FPLAPIConnector | None = None,
        feed_url: str = DEFAULT_RSS_FEED_URL,
        api_url: str = DEFAULT_FPL_BOOTSTRAP_URL,
        rss_source_id: str = "rss_feed",
        http_client: Any | None = None,
        session_factory: Any | None = None,
        llm_provider: LLMProvider | None = None,
        prediction_provider: DecisionPredictionProvider | None = None,
        task: AnalystTask | str = AnalystTask.TRANSFER_RECOMMENDATION,
        player_id: int | None = None,
        gameweek: int = 1,
        subject_label: str | None = None,
        season_code: str | None = None,
        gameweek_number: int | None = None,
    ) -> None:
        self._prediction_provider = prediction_provider or StaticPredictionProvider()
        self._task = AnalystTask(task)
        self._player_id = player_id
        self._gameweek = int(gameweek)
        self._subject_label = subject_label
        self._season_code = season_code
        self._gameweek_number = gameweek_number
        self._session_factory = session_factory or build_verification_session()
        self._llm_provider = llm_provider or make_mock_provider(
            player_names=DEFAULT_MOCK_PLAYER_NAMES
        )

        if connectors is not None:
            self._connectors = dict(connectors)
        else:
            self._connectors = {}
            if rss_connector is not None:
                self._connectors[rss_connector.name] = rss_connector
            if fpl_connector is not None:
                self._connectors[fpl_connector.name] = fpl_connector
            if not self._connectors:
                rss = RSSConnector(
                    feed_url, source_id=rss_source_id, http_client=http_client
                )
                fpl = FPLAPIConnector(api_url=api_url, http_client=http_client)
                self._connectors = {rss.name: rss, fpl.name: fpl}

    @property
    def connectors(self) -> Mapping[str, SourceConnector]:
        """The connectors the verifier runs (for inspection / cleanup)."""
        return dict(self._connectors)

    # -- public verification --------------------------------------------------

    def verify(self, *, limit: int = 10, persist: bool = True) -> EndToEndVerification:
        """Run the full pipeline once and return an :class:`EndToEndVerification`."""
        report = EndToEndVerification(layer="live_end_to_end", dry_run=not persist)
        db: Session = self._session_factory()
        ingest_results: list[tuple[str, ManualIngestReport]] = []
        scheduler: Scheduler | None = None
        try:

            def sink(raw: RawItem, *, connector: SourceConnector, dry_run: bool) -> Any:
                result = ingest_raw_text(
                    db,
                    source_id=raw.source_id,
                    text=raw.content_text,
                    published_at=raw.published_at,
                    url=raw.url,
                    title=raw.title,
                    external_id=raw.external_id,
                    source_type=connector.source_type,
                    provider=self._llm_provider,
                    season_code=self._season_code,
                    gameweek_number=self._gameweek_number,
                    dry_run=dry_run,
                )
                ingest_results.append((connector.name, result))
                return result

            recorder = RecordingNotifier()
            scheduler = Scheduler(
                self._connectors,
                ingest=sink,
                alert_generator=AlertGenerator(max_alerts_per_pass=50),
                notification_service=NotificationService(
                    [recorder], min_interval_seconds=0.0
                ),
                min_interval_seconds=0.0,
            )
            run = scheduler.run(dry_run=not persist)
        finally:
            if scheduler is not None:
                for conn in scheduler.connectors.values():
                    conn.close()
            db.close()
        connector_report = run.connector_report
        report.connector_fetched = {
            name: stats.fetched for name, stats in connector_report.runs.items()
        }
        report.total_fetched = connector_report.total_fetched
        report.connector_ingested = {
            name: sum(
                1
                for cname, result in ingest_results
                if cname == name
                and result.status in (ManualIngestStatus.CREATED, ManualIngestStatus.DUPLICATE)
            )
            for name in connector_report.runs
        }
        report.total_ingested = sum(report.connector_ingested.values())

        fetch_errors = [
            error for stats in connector_report.runs.values() for error in stats.errors
        ]
        rejected = [
            result
            for _name, result in ingest_results
            if result.status == ManualIngestStatus.REJECTED
        ]
        report.errors.extend(fetch_errors)
        report.errors.extend(
            f"rejected {result.source_id}: {result.error or result.status}"
            for result in rejected
        )

        fetched_detail = ", ".join(
            f"{name}={count}" for name, count in report.connector_fetched.items()
        )
        report.add_step(
            "fetch",
            connector_report.total_errors == 0 and report.total_fetched > 0,
            f"fetched {report.total_fetched} item(s): {fetched_detail}",
        )
        report.add_step(
            "ingest",
            connector_report.total_errors == 0 and not rejected,
            f"ingested {report.total_ingested}/{report.total_fetched} item(s) "
            "into the Phase 9.2 pipeline",
        )

        report.extraction_runs = sum(
            1 for _name, result in ingest_results if result.extraction_run_id is not None
        )
        report.availability_evidence = sum(
            result.availability_count or 0 for _name, result in ingest_results
        )
        report.tactical_evidence = sum(
            result.tactical_count or 0 for _name, result in ingest_results
        )
        report.resolved_entities = sum(
            result.resolved_count or 0 for _name, result in ingest_results
        )
        report.unresolved_evidence = sum(
            result.unresolved_count or 0 for _name, result in ingest_results
        )
        report.ambiguous_entities = sum(
            result.ambiguous_count or 0 for _name, result in ingest_results
        )
        report.add_step(
            "extract",
            report.extraction_runs > 0,
            f"{report.extraction_runs} LLM extraction run(s); "
            f"{report.availability_evidence} availability + "
            f"{report.tactical_evidence} tactical evidence draft(s)",
        )
        unresolved_handled = sum(
            len(result.unresolved_evidence_ids or []) for _name, result in ingest_results
        )
        report.add_step(
            "resolve",
            True,
            f"resolved={report.resolved_entities} "
            f"unresolved={report.unresolved_evidence} "
            f"ambiguous={report.ambiguous_entities}; "
            f"{unresolved_handled} unresolved draft(s) recorded/handled",
        )
        if not persist:
            report.add_step(
                "synthesize",
                True,
                "dry-run: evidence was rolled back, so report synthesis was skipped "
                "(re-run without --dry-run to include the report stage)",
            )
            report.add_step(
                "report",
                True,
                "dry-run: no IntelligenceReport was generated (nothing persisted)",
            )
        else:
            self._verify_synthesis(report)

        stage_errors = [
            error for error in run.errors if "alert" in error or "notif" in error
        ]
        report.errors.extend(stage_errors)
        report.alerts = len(run.alerts)
        report.notifications_delivered = (
            run.notifications.delivered if run.notifications is not None else 0
        )
        report.add_step(
            "alert",
            not stage_errors,
            f"{report.alerts} alert(s) generated; "
            f"{report.notifications_delivered} notification(s) delivered to the user",
        )
        return report

    # -- synthesis --------------------------------------------------------------

    def _verify_synthesis(self, report: EndToEndVerification) -> None:
        """Generate an IntelligenceReport over the committed evidence.

        Runs inside the same database the pipeline wrote to, so the report is
        synthesised from the evidence that was actually ingested.
        """
        synth_db: Session = self._session_factory()
        try:
            player_id = self._player_id or self._discover_subject_player(synth_db) or 1
            builder = PredictionContextBuilder(
                prediction_provider=self._prediction_provider
            )
            evidence_service = EvidenceQueryService(synth_db, allow_mock=True)
            generator = AnalystReportGenerator(
                builder,
                evidence_service,
                self._llm_provider,
                task=self._task,
                strict_leakage=False,
                allow_mock_evidence=True,
            )
            intelligence = generator.generate(
                player_id,
                self._gameweek,
                cutoff_time=utc_now(),
                subject_label=self._subject_label,
            )
            markdown = intelligence.render_markdown()
            report.player_id = player_id
            report.reports_generated = 1
            report.report_citations = len(intelligence.citations)
            report.add_step(
                "synthesize",
                True,
                f"generated {self._task.value} report for player {player_id}; "
                f"{report.report_citations} evidence citation(s) synthesised "
                "with the quantitative prediction baseline",
            )
            report.add_step(
                "report",
                "Quantitative Baseline" in markdown,
                "IntelligenceReport rendered to Markdown with the "
                "'Quantitative Baseline' section",
            )
        except Exception as exc:  # noqa: BLE001 - a verification failure, not a crash
            report.add_step("synthesize", False, f"{type(exc).__name__}: {exc}")
            report.errors.append(f"report synthesis failed: {exc}")
        finally:
            synth_db.close()

    def _discover_subject_player(self, db: Session) -> int | None:
        """Pick the player with the most persisted resolved evidence, if any."""
        best: tuple[int, int] | None = None
        for model in (AvailabilityEvidence, TacticalEvidence):
            rows = db.execute(
                select(model.player_id, func.count())
                .where(model.player_id.isnot(None))
                .group_by(model.player_id)
                .order_by(func.count().desc())
            ).all()
            for player_id, count in rows:
                if player_id is None:
                    continue
                if best is None or int(count) > best[1]:
                    best = (int(player_id), int(count))
        return best[0] if best is not None else None
