"""Phase 9 unit tests — live intelligence temporal ledger, ingestion, extraction, analyst."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.availability.models import SourceReliability
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    Gameweek,
    Player,
    Season,
)
from fpl_intelligence.domain.environment import DataEnvironment
from fpl_intelligence.features.temporal import InformationAccessPolicy
from fpl_intelligence.live_intelligence.analyst import (
    AIAnalyst,
    AnalystGuardrailError,
    EvidenceCitation,
    QuantitativeBaseline,
)
from fpl_intelligence.live_intelligence.extraction import (
    ExtractionStatus,
    LLMExtractionRun,
    PersistenceReport,
    PromptedLLMExtractor,
    persist_extraction,
    usable_drafts,
)
from fpl_intelligence.live_intelligence.ingestion import (
    IngestionStatus,
    LiveIngestionPipeline,
    RawTextSubmission,
)
from fpl_intelligence.live_intelligence.llm_providers import (
    LLMAuthError,
    LLMRateLimitError,
)
from fpl_intelligence.live_intelligence.llm_settings import LLMProviderName
from fpl_intelligence.live_intelligence.mock_llm import (
    MockLLMProvider,
    make_mock_provider,
)
from fpl_intelligence.live_intelligence.models import (
    CaptureMethod,
    LedgerTemporalClass,
    LiveIntelligenceRawItem,
    LiveIntelligenceSource,
    LiveSourceType,
)
from fpl_intelligence.live_intelligence.prompt_registry import (
    hash_prompt_template,
    verify_prompt_registry,
)
from fpl_intelligence.live_intelligence.prompts import (
    get_template,
)
from fpl_intelligence.live_intelligence.provider_router import (
    DEFAULT_TASK_ROUTES,
    ProviderRouter,
    ProviderRoutingError,
    RoutingStrategy,
)
from fpl_intelligence.live_intelligence.schemas import (
    ANALYST_SCHEMA_VERSION,
    EXTRACTION_SCHEMA_VERSION,
    quote_is_grounded,
)
from fpl_intelligence.live_intelligence.temporal_ledger import (
    LedgerTimestamps,
    TemporalIntegrityError,
    TemporalLedger,
    classify_ledger_entry,
    content_hash,
    derive_available_at,
    is_usable_for_deadline,
    is_validation_evidence,
    normalize_text,
    validate_timestamps,
)
from fpl_intelligence.optimization.provider import DecisionPredictionProvider, PlayerPrediction

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def source(db_session: Session) -> LiveIntelligenceSource:
    src = LiveIntelligenceSource(
        name="test_journalist",
        source_type=LiveSourceType.JOURNALIST,
        reliability=SourceReliability.UNVERIFIED,
        capture_method=CaptureMethod.MANUAL_PASTE,
        environment=DataEnvironment.MOCK.value,
        publication_timestamp_trusted=False,
    )
    db_session.add(src)
    db_session.flush()
    return src


# ---------------------------------------------------------------------------
# Temporal ledger
# ---------------------------------------------------------------------------


class TestTemporalLedger:
    def test_derive_available_at_consistently_conservative(self):
        published = _utc(datetime(2025, 8, 10, 9, 0, 0))
        scraped = _utc(datetime(2025, 8, 10, 10, 0, 0))
        assert derive_available_at(published, scraped) == scraped

    def test_derive_available_at_when_published_after_scraped_raises(self):
        published = _utc(datetime(2025, 8, 10, 11, 0, 0))
        scraped = _utc(datetime(2025, 8, 10, 10, 0, 0))
        # derive_available_at doesn't validate ordering; it just derives.
        # Validation happens in validate_timestamps.
        result = derive_available_at(published, scraped)
        assert result == published

    def test_derive_available_at_no_published(self):
        scraped = _utc(datetime(2025, 8, 10, 10, 0, 0))
        assert derive_available_at(None, scraped) == scraped

    def test_validate_timestamps_ordering_invariants(self):
        now = _utc(datetime(2025, 8, 10, 12, 0, 0))
        ts = LedgerTimestamps(
            scraped_at=_utc(datetime(2025, 8, 10, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 10, 11, 0, 0)),
            available_at=_utc(datetime(2025, 8, 10, 10, 30, 0)),
            published_at=_utc(datetime(2025, 8, 10, 9, 0, 0)),
            event_time=_utc(datetime(2025, 8, 10, 8, 0, 0)),
        )
        validated = validate_timestamps(ts, now=now)
        assert validated.scraped_at == ts.scraped_at

    def test_validate_timestamps_rejects_published_after_scraped(self):
        now = _utc(datetime(2025, 8, 10, 12, 0, 0))
        ts = LedgerTimestamps(
            scraped_at=_utc(datetime(2025, 8, 10, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 10, 11, 0, 0)),
            available_at=_utc(datetime(2025, 8, 10, 10, 30, 0)),
            published_at=_utc(datetime(2025, 8, 10, 11, 30, 0)),
        )
        with pytest.raises(TemporalIntegrityError, match="published_at.*after scraped_at"):
            validate_timestamps(ts, now=now)

    def test_validate_timestamps_rejects_future_available_at(self):
        future = _utc(datetime(2025, 8, 11, 12, 0, 0))
        now = _utc(datetime(2025, 8, 10, 12, 0, 0))
        ts = LedgerTimestamps(
            scraped_at=_utc(datetime(2025, 8, 10, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 10, 11, 0, 0)),
            available_at=future,
            published_at=_utc(datetime(2025, 8, 10, 9, 0, 0)),
        )
        with pytest.raises(TemporalIntegrityError, match="available_at.*after ingested_at"):
            validate_timestamps(ts, now=now)

    def test_validate_timestamps_rejects_naive(self):
        ts = LedgerTimestamps(
            scraped_at=datetime(2025, 8, 10, 10, 0, 0),
            ingested_at=datetime(2025, 8, 10, 11, 0, 0),
            available_at=datetime(2025, 8, 10, 10, 30, 0),
        )
        with pytest.raises(TemporalIntegrityError, match="must be timezone-aware"):
            validate_timestamps(ts)

    def test_classify_pre_deadline_strict_reproducibility(self):
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        ts = LedgerTimestamps(
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        )
        assert classify_ledger_entry(ts, deadline) == LedgerTemporalClass.PRE_DEADLINE

    def test_classify_post_deadline_strict_reproducibility(self):
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        ts = LedgerTimestamps(
            scraped_at=_utc(datetime(2025, 8, 18, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 18, 10, 5, 0)),
            available_at=_utc(datetime(2025, 8, 18, 10, 0, 0)),
        )
        assert classify_ledger_entry(ts, deadline) == LedgerTemporalClass.POST_DEADLINE

    def test_classify_no_deadline_context(self):
        ts = LedgerTimestamps(
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        )
        assert classify_ledger_entry(ts, None) == LedgerTemporalClass.NO_DEADLINE_CONTEXT

    def test_is_usable_for_deadline_requires_pre_deadline(self):
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        ts_pre = LedgerTimestamps(
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        )
        assert is_usable_for_deadline(ts_pre, deadline) is True

        ts_post = LedgerTimestamps(
            scraped_at=_utc(datetime(2025, 8, 18, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 18, 10, 5, 0)),
            available_at=_utc(datetime(2025, 8, 18, 10, 0, 0)),
        )
        assert is_usable_for_deadline(ts_post, deadline) is False

    def test_is_validation_evidence_requires_all_three_axes(self):
        assert (
            is_validation_evidence(
                LedgerTemporalClass.PRE_DEADLINE, "real", is_mock_extraction=False
            )
            is True
        )
        assert (
            is_validation_evidence(
                LedgerTemporalClass.POST_DEADLINE, "real", is_mock_extraction=False
            )
            is False
        )
        assert (
            is_validation_evidence(
                LedgerTemporalClass.PRE_DEADLINE, "mock", is_mock_extraction=False
            )
            is False
        )
        assert (
            is_validation_evidence(
                LedgerTemporalClass.PRE_DEADLINE, "real", is_mock_extraction=True
            )
            is False
        )

    def test_content_hash_deterministic(self):
        text = "  Haaland is  injured.  "
        assert content_hash(text) == content_hash("Haaland is injured.")

    def test_content_hash_differs(self):
        assert content_hash("Haaland is injured") != content_hash("Salah is injured")

    def test_normalize_text(self):
        assert normalize_text("  Hello   world  ") == "Hello world"


class TestTemporalLedgerService:
    def test_find_by_hash_returns_existing(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        ledger = TemporalLedger(db_session)
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Salah returns to training."),
            raw_text="Salah returns to training.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
        )
        db_session.add(raw)
        db_session.flush()
        found = ledger.find_by_hash(source.id, content_hash("Salah returns to training."))
        assert found is not None
        assert found.id == raw.id

    def test_to_view_projects_read_only(self, db_session: Session, source: LiveIntelligenceSource):
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Test text."),
            raw_text="Test text.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        )
        db_session.add(raw)
        db_session.flush()
        ledger = TemporalLedger(db_session)
        view = ledger.to_view(raw)
        assert view.raw_item_id == raw.id
        assert view.environment == DataEnvironment.MOCK.value
        assert view.temporal_class == LedgerTemporalClass.PRE_DEADLINE

    def test_items_available_before_filters_by_policy(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        cutoff = _utc(datetime(2025, 8, 16, 12, 0, 0))
        for i, (avail, ing) in enumerate([
            (_utc(datetime(2025, 8, 15, 10, 0, 0)), _utc(datetime(2025, 8, 15, 10, 5, 0))),
            (_utc(datetime(2025, 8, 16, 10, 0, 0)), _utc(datetime(2025, 8, 16, 11, 0, 0))),
        ]):
            raw = LiveIntelligenceRawItem(
                source_id=source.id,
                content_hash=content_hash(f"text {i}"),
                raw_text=f"text {i}",
                scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
                available_at=avail,
                ingested_at=ing,
            )
            db_session.add(raw)
        db_session.flush()
        ledger = TemporalLedger(db_session, policy=InformationAccessPolicy.STRICT_REPRODUCIBILITY)
        items = ledger.items_available_before(cutoff)
        assert len(items) == 2

    def test_attach_deadline_classifies_no_deadline_row(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Test."),
            raw_text="Test.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.NO_DEADLINE_CONTEXT,
        )
        db_session.add(raw)
        db_session.flush()
        ledger = TemporalLedger(db_session)
        result = ledger.attach_deadline(raw, deadline)
        assert result == LedgerTemporalClass.PRE_DEADLINE
        assert raw.deadline_at == deadline
        assert raw.temporal_class == LedgerTemporalClass.PRE_DEADLINE

    def test_attach_deadline_rejects_already_classified(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Test."),
            raw_text="Test.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.POST_DEADLINE,
        )
        db_session.add(raw)
        db_session.flush()
        ledger = TemporalLedger(db_session)
        with pytest.raises(TemporalIntegrityError, match="already classified"):
            ledger.attach_deadline(raw, deadline)


# ---------------------------------------------------------------------------
# Ingestion pipeline
# ---------------------------------------------------------------------------


class TestLiveIngestionPipeline:
    def test_register_source_creates_and_returns(self, db_session: Session):
        pipeline = LiveIngestionPipeline(db_session, default_environment=DataEnvironment.MOCK)
        src = pipeline.register_source("my_source", source_type=LiveSourceType.JOURNALIST)
        assert src.id is not None
        assert src.environment == DataEnvironment.MOCK.value

        # idempotent
        src2 = pipeline.register_source("my_source")
        assert src2.id == src.id

    def test_ingest_creates_row(self, db_session: Session):
        pipeline = LiveIngestionPipeline(db_session, default_environment=DataEnvironment.REAL)
        pipeline.register_source("real_source", environment=DataEnvironment.REAL)
        submission = RawTextSubmission(
            source_name="real_source",
            raw_text="Salah is injured.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            published_at=_utc(datetime(2025, 8, 15, 9, 30, 0)),
        )
        outcome = pipeline.ingest(submission)
        assert outcome.status is IngestionStatus.CREATED
        assert outcome.raw_item_id is not None
        assert outcome.temporal_class == LedgerTemporalClass.NO_DEADLINE_CONTEXT

        item = db_session.get(LiveIntelligenceRawItem, outcome.raw_item_id)
        assert item is not None
        assert item.available_at >= item.scraped_at

    def test_ingest_detects_duplicate(self, db_session: Session):
        pipeline = LiveIngestionPipeline(db_session)
        pipeline.register_source("dup_source")
        text = "Duplicate content."
        sub1 = RawTextSubmission(
            source_name="dup_source",
            raw_text=text,
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        )
        sub2 = RawTextSubmission(
            source_name="dup_source",
            raw_text=text,
            scraped_at=_utc(datetime(2025, 8, 15, 11, 0, 0)),
        )
        o1 = pipeline.ingest(sub1)
        o2 = pipeline.ingest(sub2)
        assert o1.status is IngestionStatus.CREATED
        assert o2.status is IngestionStatus.DUPLICATE
        assert o2.raw_item_id == o1.raw_item_id

    def test_ingest_rejects_empty_text(self, db_session: Session):
        pipeline = LiveIngestionPipeline(db_session)
        pipeline.register_source("src")
        outcome = pipeline.ingest(RawTextSubmission(
            source_name="src", raw_text="   ", scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        ))
        assert outcome.status is IngestionStatus.REJECTED
        assert "empty" in outcome.reason

    def test_ingest_rejects_unknown_source(self, db_session: Session):
        pipeline = LiveIngestionPipeline(db_session)
        outcome = pipeline.ingest(RawTextSubmission(
            source_name="no_such_source",
            raw_text="Hello",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        ))
        assert outcome.status is IngestionStatus.REJECTED
        assert "unknown source" in outcome.reason

    def test_ingest_classifies_pre_deadline_with_gameweek(self, db_session: Session):
        fixed_clock_time = _utc(datetime(2025, 8, 15, 10, 0, 0))
        pipeline = LiveIngestionPipeline(db_session, clock=lambda: fixed_clock_time)
        pipeline.register_source("src")
        season = Season(
            code="2025-26",
            display_name="2025/26",
            start_date=_utc(datetime(2025, 8, 1)),
            end_date=_utc(datetime(2026, 5, 31)),
        )
        db_session.add(season)
        db_session.flush()
        gw = Gameweek(
            season_id=season.id,
            provider_event_id=1,
            name="GW1",
            deadline_time=_utc(datetime(2025, 8, 15, 18, 30, 0)),
        )
        db_session.add(gw)
        db_session.flush()

        submission = RawTextSubmission(
            source_name="src",
            raw_text="Salah is injured.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            season_code="2025-26",
            gameweek_number=1,
        )
        outcome = pipeline.ingest(submission)
        assert outcome.status is IngestionStatus.CREATED
        assert outcome.temporal_class == LedgerTemporalClass.PRE_DEADLINE
        assert outcome.deadline_at == gw.deadline_time

    def test_ingest_report_conservation(self, db_session: Session):
        pipeline = LiveIngestionPipeline(db_session)
        pipeline.register_source("src")
        submissions = [
            RawTextSubmission(
                source_name="src",
                raw_text=f"text {i}",
                scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            )
            for i in range(3)
        ]
        report = pipeline.ingest_many(submissions)
        assert report.conservation_ok() is True
        assert report.submitted == 3
        assert report.created == 3


# ---------------------------------------------------------------------------
# LLM extraction (mock provider)
# ---------------------------------------------------------------------------


class TestLLMExtractionMock:
    def test_mock_provider_is_mock(self):
        provider = make_mock_provider(player_names=["Salah"])
        assert provider.is_mock is True
        assert provider.provider_name == "mock"

    def test_mock_generates_availability_for_keyword(self):
        provider = make_mock_provider(player_names=["Salah"])
        prompt = get_template("phase9.extract.availability").render(
            context={
                "raw_text": "Salah is injured.",
                "source_name": "src",
                "source_type": "journalist",
                "source_reliability": "unverified",
                "team_hint": "LIV",
            },
            raw_text="Salah is injured.",
            source_name="src",
            source_type="journalist",
            source_reliability="unverified",
            team_hint="LIV",
        )
        response = provider.complete(prompt)
        payload = json.loads(response.text)
        assert len(payload["availability_evidence"]) == 1
        assert payload["availability_evidence"][0]["player_name"] == "Salah"
        assert payload["tactical_evidence"] == []

    def test_mock_generates_tactical_for_keyword(self):
        provider = make_mock_provider(player_names=["Salah"])
        prompt = get_template("phase9.extract.tactical").render(
            context={
                "raw_text": "Salah takes penalties.",
                "source_name": "src",
                "source_type": "journalist",
                "source_reliability": "unverified",
                "team_hint": "LIV",
            },
            raw_text="Salah takes penalties.",
            source_name="src",
            source_type="journalist",
            source_reliability="unverified",
            team_hint="LIV",
        )
        response = provider.complete(prompt)
        payload = json.loads(response.text)
        assert len(payload["tactical_evidence"]) == 1
        assert payload["tactical_evidence"][0]["evidence_type"] == "set_piece_penalties"

    def test_extractor_validates_schema(self, db_session: Session, source: LiveIntelligenceSource):
        provider = make_mock_provider(player_names=["Salah"])
        extractor = PromptedLLMExtractor(provider)
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Salah is injured."),
            raw_text="Salah is injured.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        )
        db_session.add(raw)
        db_session.flush()
        view = TemporalLedger(db_session).to_view(raw)
        result = extractor.extract(view)
        assert result.status.value == "ok"
        assert len(result.availability) == 1
        assert result.availability[0].player_name == "Salah"
        assert result.availability[0].temporal_class == LedgerTemporalClass.PRE_DEADLINE

    def test_extractor_rejects_ungrounded_quote(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        provider = make_mock_provider(player_names=["Salah"], scripted={"*": json.dumps({
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "availability_evidence": [{
                "player_name": "Salah",
                "team_name": "LIV",
                "evidence_type": "injury",
                "status_mentioned": "out",
                "confidence": 0.9,
                "source_quote": "This quote does not appear in the text.",
                "reasoning": "test",
            }],
            "tactical_evidence": [],
            "no_evidence_found": False,
            "extraction_notes": "",
        })})
        extractor = PromptedLLMExtractor(provider)
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Salah is injured."),
            raw_text="Salah is injured.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        )
        db_session.add(raw)
        db_session.flush()
        view = TemporalLedger(db_session).to_view(raw)
        result = extractor.extract(view)
        assert result.status == ExtractionStatus.GROUNDING_REJECTED
        assert len(result.rejected) == 1

    def test_extractor_handles_malformed_json(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        provider = make_mock_provider(player_names=["Salah"], scripted={"*": "NOT JSON"})
        extractor = PromptedLLMExtractor(provider)
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Salah is injured."),
            raw_text="Salah is injured.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        )
        db_session.add(raw)
        db_session.flush()
        view = TemporalLedger(db_session).to_view(raw)
        result = extractor.extract(view)
        assert result.status == ExtractionStatus.PARSE_FAILED

    def test_extractor_rejects_extra_keys(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        provider = make_mock_provider(player_names=["Salah"], scripted={"*": json.dumps({
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "availability_evidence": [],
            "tactical_evidence": [],
            "no_evidence_found": True,
            "extraction_notes": "",
            "invented_field": "bad",
        })})
        extractor = PromptedLLMExtractor(provider)
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Salah is injured."),
            raw_text="Salah is injured.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        )
        db_session.add(raw)
        db_session.flush()
        view = TemporalLedger(db_session).to_view(raw)
        result = extractor.extract(view)
        assert result.status == ExtractionStatus.SCHEMA_REJECTED

    def test_extractor_accepts_available_status(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        """Phase 9.1.1 — 'available' is now a canonical status and must pass."""
        provider = make_mock_provider(player_names=["Salah"], scripted={"*": json.dumps({
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "availability_evidence": [{
                "player_name": "Salah",
                "team_name": "LIV",
                "evidence_type": "fitness",
                "status_mentioned": "available",
                "confidence": 0.7,
                "source_quote": "Salah trained fully and is available.",
                "reasoning": "trained fully but not confirmed to start",
            }],
            "tactical_evidence": [],
            "no_evidence_found": False,
            "extraction_notes": "",
        })})
        extractor = PromptedLLMExtractor(provider)
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Salah trained fully and is available."),
            raw_text="Salah trained fully and is available.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        )
        db_session.add(raw)
        db_session.flush()
        view = TemporalLedger(db_session).to_view(raw)
        result = extractor.extract(view)
        assert result.status == ExtractionStatus.OK
        assert len(result.availability) == 1
        assert result.availability[0].status_mentioned == "available"

    def test_extractor_rejects_invalid_status(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        """Phase 9.1.1 — a non-canonical status like 'flying' must be rejected."""
        provider = make_mock_provider(player_names=["Salah"], scripted={"*": json.dumps({
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "availability_evidence": [{
                "player_name": "Salah",
                "team_name": "LIV",
                "evidence_type": "fitness",
                "status_mentioned": "flying",
                "confidence": 0.7,
                "source_quote": "Salah is flying.",
                "reasoning": "made up",
            }],
            "tactical_evidence": [],
            "no_evidence_found": False,
            "extraction_notes": "",
        })})
        extractor = PromptedLLMExtractor(provider)
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Salah is flying."),
            raw_text="Salah is flying.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        )
        db_session.add(raw)
        db_session.flush()
        view = TemporalLedger(db_session).to_view(raw)
        result = extractor.extract(view)
        assert result.status == ExtractionStatus.SCHEMA_REJECTED
        assert result.availability == []

    def test_usable_drafts_filters_pre_deadline_only(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        provider = make_mock_provider(player_names=["Salah"])
        extractor = PromptedLLMExtractor(provider)
        raw_pre = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Salah is injured."),
            raw_text="Salah is injured.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        )
        raw_post = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Salah is fit."),
            raw_text="Salah is fit.",
            scraped_at=_utc(datetime(2025, 8, 18, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 18, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 18, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.POST_DEADLINE,
        )
        db_session.add_all([raw_pre, raw_post])
        db_session.flush()
        ledger = TemporalLedger(db_session)
        view_pre = ledger.to_view(raw_pre)
        view_post = ledger.to_view(raw_post)
        res_pre = extractor.extract(view_pre)
        res_post = extractor.extract(view_post)
        avail_pre, _ = usable_drafts(res_pre)
        avail_post, _ = usable_drafts(res_post)
        assert len(avail_pre) == 1
        assert len(avail_post) == 0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistExtraction:
    def test_persist_creates_run_and_evidence(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        provider = make_mock_provider(player_names=["Salah"])
        extractor = PromptedLLMExtractor(provider)
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Salah is injured."),
            raw_text="Salah is injured.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        )
        db_session.add(raw)
        db_session.flush()

        season = Season(code="2025-26", display_name="2025/26")
        db_session.add(season)
        db_session.flush()

        player = Player(first_name="Mohamed", second_name="Salah", web_name="Salah")
        db_session.add(player)
        db_session.flush()

        view = TemporalLedger(db_session).to_view(raw)
        result = extractor.extract(view)
        assert result.ok

        def resolve_player(name: str, team: str | None) -> int | None:
            if name == "Salah":
                return player.id
            return None

        report: PersistenceReport = persist_extraction(
            db_session, result, season_id=season.id, resolve_player=resolve_player,
        )
        assert report.availability_persisted == 1
        assert report.extraction_run_id is not None
        run = db_session.get(LLMExtractionRun, report.extraction_run_id)
        assert run.is_mock is True
        assert run.availability_evidence_count == 1

    def test_persist_records_unresolved_player(
        self, db_session: Session, source: LiveIntelligenceSource,
    ):
        scripted = json.dumps({
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "availability_evidence": [{
                "player_name": "Unknown Player",
                "team_name": None,
                "evidence_type": "injury",
                "status_mentioned": "out",
                "confidence": 0.9,
                "source_quote": "Unknown Player is injured.",
                "reasoning": "test",
            }],
            "tactical_evidence": [],
            "no_evidence_found": False,
            "extraction_notes": "",
        })
        provider = make_mock_provider(player_names=[], scripted={"*": scripted})
        extractor = PromptedLLMExtractor(provider)
        raw = LiveIntelligenceRawItem(
            source_id=source.id,
            content_hash=content_hash("Unknown Player is injured."),
            raw_text="Unknown Player is injured.",
            scraped_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
            ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
            temporal_class=LedgerTemporalClass.PRE_DEADLINE,
        )
        db_session.add(raw)
        db_session.flush()

        season = Season(code="2025-26", display_name="2025/26")
        db_session.add(season)
        db_session.flush()

        view = TemporalLedger(db_session).to_view(raw)
        result = extractor.extract(view)
        report: PersistenceReport = persist_extraction(
            db_session, result, season_id=season.id,
        )
        assert report.availability_persisted == 0
        assert len(report.unresolved) == 1
        assert report.unresolved[0]["reason"] == "player could not be resolved to a canonical id"


# ---------------------------------------------------------------------------
# Analyst guardrails
# ---------------------------------------------------------------------------


class MockPredictionProvider(DecisionPredictionProvider):
    def __init__(self, predictions: dict[tuple[int, int], PlayerPrediction]):
        self._predictions = predictions

    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        return self._predictions[(player_id, gameweek)]

    def get_squad_predictions(
        self, squad_players: list[int], gameweeks: list[int],
    ) -> dict[int, dict[int, PlayerPrediction]]:
        return {}

    def get_all_predictions(self, gameweek: int) -> dict[int, PlayerPrediction]:
        return {}

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        return 1


def _make_prediction(player_id: int, gameweek: int, **overrides: Any) -> PlayerPrediction:
    defaults = dict(
        player_id=player_id,
        gameweek=gameweek,
        expected_points=5.5,
        expected_minutes=60.0,
        start_probability=0.8,
        distribution=np.array([5.5]),
        floor=2.0,
        ceiling=10.0,
        confidence=0.9,
        data_completeness=1.0,
    )
    defaults.update(overrides)
    return PlayerPrediction(**defaults)


class TestAIAnalyst:
    def test_mock_provider_restates_baseline_verbatim(self):
        provider = make_mock_provider()
        analyst = AIAnalyst(provider)
        baseline = QuantitativeBaseline(
            subject_ref="player:1",
            player_id=1,
            gameweek=1,
            expected_points=5.5,
            expected_minutes=60.0,
            start_probability=0.8,
            floor=2.0,
            ceiling=10.0,
        )
        evidence = [
            EvidenceCitation(
                evidence_ref="ev_1",
                kind="availability",
                summary="Salah is injured.",
                source_name="test",
                source_reliability="unverified",
                confidence=0.9,
                available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
                ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
                temporal_class=LedgerTemporalClass.PRE_DEADLINE,
                is_mock=True,
            )
        ]
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        report = analyst.transfer_recommendation(
            baselines=[baseline],
            evidence=evidence,
            subject_label="Salah",
            gameweek=1,
            deadline=deadline,
        )
        assert report.output.quantitative_baseline[0].expected_points == 5.5
        # mock provider derives direction from evidence; with unknown direction it returns neutral
        assert report.output.qualitative_adjustment.direction == "neutral"

    def test_empty_evidence_forces_neutral_adjustment(self):
        provider = make_mock_provider()
        analyst = AIAnalyst(provider)
        baseline = QuantitativeBaseline(
            subject_ref="player:1",
            player_id=1,
            gameweek=1,
            expected_points=5.5,
            expected_minutes=60.0,
            start_probability=0.8,
            floor=2.0,
            ceiling=10.0,
        )
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        report = analyst.transfer_recommendation(
            baselines=[baseline],
            evidence=[],
            subject_label="Salah",
            gameweek=1,
            deadline=deadline,
        )
        assert report.output.qualitative_adjustment.direction == "neutral"
        assert report.output.qualitative_adjustment.magnitude == "none"
        assert report.output.qualitative_adjustment.cited_evidence_refs == []

    def test_guardrail_rejects_unaltered_baseline(self):
        class BadProvider(MockLLMProvider):
            def _generate_analyst(self, prompt):
                return json.dumps({
                    "schema_version": ANALYST_SCHEMA_VERSION,
                    "task": "transfer_recommendation",
                    "headline": "Bad",
                    "quantitative_baseline": [{
                        "subject_ref": "player:1",
                        "expected_points": 99.9,
                        "start_probability": 0.8,
                        "floor": 2.0,
                        "ceiling": 10.0,
                        "interpretation": "wrong",
                    }],
                    "qualitative_adjustment": {
                        "direction": "neutral",
                        "magnitude": "none",
                        "cited_evidence_refs": [],
                        "rationale": "",
                    },
                    "net_assessment": "",
                    "recommendation": "hold",
                    "confidence": 0.5,
                    "caveats": [],
                })

        analyst = AIAnalyst(BadProvider())
        baseline = QuantitativeBaseline(
            subject_ref="player:1",
            player_id=1,
            gameweek=1,
            expected_points=5.5,
            expected_minutes=60.0,
            start_probability=0.8,
            floor=2.0,
            ceiling=10.0,
        )
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        with pytest.raises(AnalystGuardrailError, match="altered the quantitative baseline"):
            analyst.transfer_recommendation(
                baselines=[baseline],
                evidence=[],
                subject_label="Salah",
                gameweek=1,
                deadline=deadline,
            )

    def test_guardrail_rejects_missing_citation(self):
        class BadProvider(MockLLMProvider):
            def _generate_analyst(self, prompt):
                return json.dumps({
                    "schema_version": ANALYST_SCHEMA_VERSION,
                    "task": "transfer_recommendation",
                    "headline": "Bad",
                    "quantitative_baseline": [{
                        "subject_ref": "player:1",
                        "expected_points": 5.5,
                        "start_probability": 0.8,
                        "floor": 2.0,
                        "ceiling": 10.0,
                        "interpretation": "",
                    }],
                    "qualitative_adjustment": {
                        "direction": "down",
                        "magnitude": "moderate",
                        "cited_evidence_refs": ["ev_nonexistent"],
                        "rationale": "bad",
                    },
                    "net_assessment": "",
                    "recommendation": "avoid",
                    "confidence": 0.5,
                    "caveats": [],
                })

        analyst = AIAnalyst(BadProvider())
        baseline = QuantitativeBaseline(
            subject_ref="player:1",
            player_id=1,
            gameweek=1,
            expected_points=5.5,
            expected_minutes=60.0,
            start_probability=0.8,
            floor=2.0,
            ceiling=10.0,
        )
        evidence = [
            EvidenceCitation(
                evidence_ref="ev_1",
                kind="availability",
                summary="Salah is injured.",
                source_name="test",
                source_reliability="unverified",
                confidence=0.9,
                available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
                ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
                temporal_class=LedgerTemporalClass.PRE_DEADLINE,
            )
        ]
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        with pytest.raises(AnalystGuardrailError, match="hallucinated citation"):
            analyst.transfer_recommendation(
                baselines=[baseline],
                evidence=evidence,
                subject_label="Salah",
                gameweek=1,
                deadline=deadline,
            )

    def test_mock_evidence_excluded_by_default(self):
        provider = make_mock_provider()
        analyst = AIAnalyst(provider, allow_mock_evidence=False)
        baseline = QuantitativeBaseline(
            subject_ref="player:1",
            player_id=1,
            gameweek=1,
            expected_points=5.5,
            expected_minutes=60.0,
            start_probability=0.8,
            floor=2.0,
            ceiling=10.0,
        )
        evidence = [
            EvidenceCitation(
                evidence_ref="ev_1",
                kind="availability",
                summary="mock",
                source_name="test",
                source_reliability="unverified",
                confidence=0.9,
                available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
                ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
                temporal_class=LedgerTemporalClass.PRE_DEADLINE,
                is_mock=True,
            )
        ]
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        report = analyst.transfer_recommendation(
            baselines=[baseline],
            evidence=evidence,
            subject_label="Salah",
            gameweek=1,
            deadline=deadline,
        )
        assert report.output.qualitative_adjustment.direction == "neutral"
        assert report.output.qualitative_adjustment.magnitude == "none"
        assert any(
            e["reason"].startswith("was produced by a mock")
            for e in report.excluded_evidence
        )

    def test_post_deadline_evidence_raises_in_strict_mode(self):
        provider = make_mock_provider()
        analyst = AIAnalyst(provider, strict_leakage=True)
        baseline = QuantitativeBaseline(
            subject_ref="player:1",
            player_id=1,
            gameweek=1,
            expected_points=5.5,
            expected_minutes=60.0,
            start_probability=0.8,
            floor=2.0,
            ceiling=10.0,
        )
        evidence = [
            EvidenceCitation(
                evidence_ref="ev_1",
                kind="availability",
                summary="late",
                source_name="test",
                source_reliability="unverified",
                confidence=0.9,
                available_at=_utc(datetime(2025, 8, 18, 10, 0, 0)),
                ingested_at=_utc(datetime(2025, 8, 18, 10, 5, 0)),
                temporal_class=LedgerTemporalClass.POST_DEADLINE,
            )
        ]
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        with pytest.raises(Exception, match="post-deadline|look-ahead"):
            analyst.transfer_recommendation(
                baselines=[baseline],
                evidence=evidence,
                subject_label="Salah",
                gameweek=1,
                deadline=deadline,
            )


# ---------------------------------------------------------------------------
# Quote grounding
# ---------------------------------------------------------------------------


class TestQuoteGrounding:
    def test_exact_match(self):
        assert quote_is_grounded("Salah is injured", "Salah is injured.") is True

    def test_whitespace_normalised(self):
        assert quote_is_grounded("  Salah   is  injured ", "Salah is injured.") is True

    def test_case_insensitive(self):
        assert quote_is_grounded("SALAH IS INJURED", "Salah is injured.") is True

    def test_missing_quote_fails(self):
        assert quote_is_grounded("Salah is fit", "Salah is injured.") is False

    def test_empty_quote_fails(self):
        assert quote_is_grounded("", "Salah is injured.") is False


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


class TestPromptTemplates:
    def test_schema_version_rendered_in_shared_rules(self):
        template = get_template("phase9.extract.availability")
        assert EXTRACTION_SCHEMA_VERSION in template.system
        assert '"phase9.extraction.v1"' in template.system

    def test_template_hash_changes_with_content(self):
        dummy = {
            "raw_text": "test", "source_name": "s", "source_type": "t",
            "source_reliability": "u", "team_hint": "th",
        }
        t1 = get_template("phase9.extract.availability").render(**dummy)
        t2 = get_template("phase9.extract.tactical").render(**dummy)
        assert t1.hash() != t2.hash()

    def test_analyst_templates_exist(self):
        from fpl_intelligence.live_intelligence.analyst_prompts import (
            CAPTAINCY_DEBATE,
            DIFFERENTIAL_RISK,
            TRANSFER_RECOMMENDATION,
        )
        assert TRANSFER_RECOMMENDATION.template_id == "phase9.analyst.transfer"
        assert CAPTAINCY_DEBATE.template_id == "phase9.analyst.captaincy"
        assert DIFFERENTIAL_RISK.template_id == "phase9.analyst.differential"

    def test_extraction_templates_bumped_for_available(self):
        """Phase 9.1.1 — extraction prompts list 'available' and were bumped."""
        availability = get_template("phase9.extract.availability")
        combined = get_template("phase9.extract.combined")
        for template in (availability, combined):
            assert template.version == "1.1.0"
            assert '"available"' in template.system
            assert '"flying"' not in template.system
        # The schema block enumerates the canonical statuses from the enum, so
        # 'available' shows up in the exact field the model must fill.
        assert '"available"' in availability.system

    def test_prompt_registry_lock_deterministic(self):
        """Phase 9.1.1 — hashing is deterministic and the lock is current."""
        # Deterministic: same template -> same hash, repeats included.
        t = get_template("phase9.extract.availability")
        assert hash_prompt_template(t) == hash_prompt_template(t)
        # The live lock matches the computed hashes (no silent drift), which is
        # what lets the dry-run and audit trust the pinned template identity.
        assert verify_prompt_registry() == []


# ---------------------------------------------------------------------------
# Phase 9.1 ProviderRouter — task routing, fallback, round-robin, no-key
# ---------------------------------------------------------------------------


class _NoKeySettings:
    """Offline settings double for the router's ``factory.settings``."""

    def __init__(self, configured: set[LLMProviderName]) -> None:
        self._configured = set(configured)
        self.llm_max_calls_per_run = 8
        self.llm_min_seconds_between_calls = 0.0

    def has_api_key(self, provider: LLMProviderName) -> bool:
        return provider in self._configured

    def model_for(self, provider: LLMProviderName) -> str:
        return f"fake-{provider.value}"


class _FakeRouterFactory:
    """ProviderFactory double that returns mock providers without env keys."""

    def __init__(
        self,
        configured: set[LLMProviderName] | None = None,
        providers: dict[LLMProviderName, Any] | None = None,
    ) -> None:
        self.settings = _NoKeySettings(configured or set())
        self._providers = dict(providers or {})

    def create(self, provider: LLMProviderName | str, **kwargs: Any) -> Any:
        provider = LLMProviderName(provider)
        return self._providers.get(provider) or make_mock_provider()

    def default_cache(self) -> Any:
        from fpl_intelligence.live_intelligence.response_cache import NullResponseCache

        return NullResponseCache()


class _FailingProvider:
    """Offline provider that raises a configured error on every call."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def complete(self, prompt: Any) -> None:
        raise self._exc

    def close(self) -> None:
        pass


class _CountingProvider:
    """Offline provider that reports a given ``live_calls`` count (Phase 9.1.1).

    Mirrors the ``live_calls`` attribute on :class:`RealLLMProvider` so router
    usage accounting can be tested without any network access.
    """

    def __init__(self, n_live_calls: int) -> None:
        self.live_calls = n_live_calls
        self._inner = make_mock_provider()

    def complete(self, prompt: Any) -> Any:
        return self._inner.complete(prompt)

    def close(self) -> None:
        pass


class _RouterPrompt:
    """Minimal stand-in for LLMPrompt carrying only what routing needs."""

    def __init__(self, template_id: str) -> None:
        self.template_id = template_id
        self.context: dict[str, Any] = {"raw_text": "", "team_hint": None}

    def hash(self) -> str:
        return f"hash-{self.template_id}"


_ALL_PR = {LLMProviderName.GROQ, LLMProviderName.GEMINI, LLMProviderName.OPENROUTER}


class TestProviderRouter:
    def test_default_task_routes(self):
        assert DEFAULT_TASK_ROUTES["availability"] is LLMProviderName.GROQ
        assert DEFAULT_TASK_ROUTES["tactical"] is LLMProviderName.GEMINI
        assert DEFAULT_TASK_ROUTES["combined"] is LLMProviderName.GEMINI

    def test_task_based_routing_picks_preferred_provider(self):
        factory = _FakeRouterFactory(configured=_ALL_PR)
        router = ProviderRouter(factory)
        template = get_template("phase9.extract.availability")

        decision = router.route("availability", template, prompt_hash="h1")
        assert decision.provider_name == "groq"
        assert decision.routing_strategy is RoutingStrategy.TASK_BASED
        assert decision.task == "availability"
        assert decision.template_id == template.template_id
        assert decision.prompt_hash == "h1"

        tactical = router.route("tactical", template, prompt_hash="h2")
        assert tactical.provider_name == "gemini"
        assert tactical.routing_strategy is RoutingStrategy.TASK_BASED

    def test_round_robin_cycles_providers(self):
        factory = _FakeRouterFactory(configured=_ALL_PR)
        router = ProviderRouter(factory, round_robin=True)
        template = get_template("phase9.extract.availability")

        d1 = router.route("availability", template, prompt_hash="h")
        d2 = router.route("availability", template, prompt_hash="h")
        assert d1.routing_strategy is RoutingStrategy.ROUND_ROBIN
        assert d2.routing_strategy is RoutingStrategy.ROUND_ROBIN
        assert d1.provider_name != d2.provider_name
        assert {d1.provider_name, d2.provider_name} == {"groq", "gemini"}

    def test_route_prefers_tactic_preferred_when_configured(self):
        factory = _FakeRouterFactory(configured={LLMProviderName.GEMINI})
        router = ProviderRouter(factory)
        template = get_template("phase9.extract.tactical")

        # tactical prefers gemini, which IS configured → direct task routing.
        decision = router.route("tactical", template, prompt_hash="h")
        assert decision.provider_name == "gemini"
        assert decision.routing_strategy is RoutingStrategy.TASK_BASED

    def test_no_key_raises_provider_routing_error(self):
        factory = _FakeRouterFactory(configured=set())
        router = ProviderRouter(factory)
        template = get_template("phase9.extract.availability")
        with pytest.raises(ProviderRoutingError, match="API key"):
            router.route("availability", template, prompt_hash="h")
        with pytest.raises(ProviderRoutingError, match="API key"):
            router.complete(_RouterPrompt("phase9.extract.availability"))

    def test_fallback_on_rate_limit(self):
        factory = _FakeRouterFactory(
            configured={LLMProviderName.GROQ, LLMProviderName.GEMINI},
            providers={LLMProviderName.GROQ: _FailingProvider(LLMRateLimitError("429"))},
        )
        router = ProviderRouter(factory)
        response = router.complete(_RouterPrompt("phase9.extract.availability"))

        assert router.last_route is not None
        assert router.last_route.provider_name == "gemini"
        assert router.last_route.routing_strategy is RoutingStrategy.FALLBACK
        assert response.routing_strategy == "fallback"

    def test_fallback_on_auth_error(self):
        factory = _FakeRouterFactory(
            configured={LLMProviderName.GROQ, LLMProviderName.GEMINI},
            providers={LLMProviderName.GROQ: _FailingProvider(LLMAuthError("401"))},
        )
        router = ProviderRouter(factory)
        response = router.complete(_RouterPrompt("phase9.extract.availability"))

        assert router.last_route is not None
        assert router.last_route.provider_name == "gemini"
        assert router.last_route.routing_strategy is RoutingStrategy.FALLBACK
        assert response.routing_strategy == "fallback"

    def test_all_providers_fail_raises(self):
        factory = _FakeRouterFactory(
            configured={LLMProviderName.GROQ, LLMProviderName.GEMINI},
            providers={
                LLMProviderName.GROQ: _FailingProvider(LLMRateLimitError("429")),
                LLMProviderName.GEMINI: _FailingProvider(LLMAuthError("401")),
            },
        )
        router = ProviderRouter(factory)
        with pytest.raises(ProviderRoutingError, match="All providers failed"):
            router.complete(_RouterPrompt("phase9.extract.availability"))

    def test_router_holds_no_api_keys(self):
        factory = _FakeRouterFactory(configured=_ALL_PR)
        router = ProviderRouter(factory)
        assert not hasattr(router, "api_key")
        # The router never reads a key itself; it delegates to the factory.
        assert router.model_name == "router"

    def test_complete_stamps_task_based_routing_on_response(self):
        factory = _FakeRouterFactory(configured=_ALL_PR)
        router = ProviderRouter(factory)
        # tactical → gemini (configured) → mock succeeds offline.
        response = router.complete(_RouterPrompt("phase9.extract.tactical"))
        assert response.routing_strategy == "task_based"
        assert router.last_route is not None
        assert router.last_route.provider_name == "gemini"

    def test_router_tracks_live_calls(self):
        """Phase 9.1.1 — the router folds delegate providers' live-call counts in."""
        factory = _FakeRouterFactory(
            configured={LLMProviderName.GROQ, LLMProviderName.GEMINI},
            providers={
                LLMProviderName.GROQ: _CountingProvider(2),
                LLMProviderName.GEMINI: _CountingProvider(3),
            },
        )
        router = ProviderRouter(factory)
        # availability → groq (preferred), which reports 2 live calls.
        router.complete(_RouterPrompt("phase9.extract.availability"))
        assert router.live_calls == 2
        assert router.last_failure is None  # no fallback needed

    def test_fallback_records_failure_and_counts_calls(self):
        """Phase 9.1.1 — fallback surfaces primary + reason and counts both calls."""
        factory = _FakeRouterFactory(
            configured={LLMProviderName.GROQ, LLMProviderName.GEMINI},
            providers={
                LLMProviderName.GROQ: _FailingProvider(LLMRateLimitError("429")),
                LLMProviderName.GEMINI: _CountingProvider(3),
            },
        )
        router = ProviderRouter(factory)
        response = router.complete(_RouterPrompt("phase9.extract.availability"))

        assert router.last_route is not None
        assert router.last_route.provider_name == "gemini"
        assert router.last_route.routing_strategy is RoutingStrategy.FALLBACK
        assert response.routing_strategy == "fallback"
        # Diagnostics: which primary was attempted and why it failed. No secrets.
        assert router.last_failure is not None
        assert router.last_failure.primary_provider == "groq"
        assert router.last_failure.reason == "rate_limit"
        # The successful fallback provider's live calls are counted.
        assert router.live_calls == 3
