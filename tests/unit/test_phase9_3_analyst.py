"""Phase 9.3 unit tests — IntelligenceReport, PredictionContext, AIAnalyst.generate_report.

All tests use MockLLMProvider (no network, no API keys). The mock provider is
configured with ``scripted`` or allows mock evidence so the analyst can produce
a valid report without real evidence persistence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.availability.models import SourceReliability
from fpl_intelligence.db.base import Base
from fpl_intelligence.domain.environment import DataEnvironment
from fpl_intelligence.live_intelligence.analyst import (
    AIAnalyst,
    AnalystGuardrailError,
    EvidenceCitation,
)
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider
from fpl_intelligence.live_intelligence.models import (
    CaptureMethod,
    LedgerTemporalClass,
    LiveIntelligenceSource,
    LiveSourceType,
)
from fpl_intelligence.live_intelligence.report import (
    IntelligenceReport,
    PredictionContext,
    ReportConfidence,
    ReportEvidenceCitation,
    ReportQualitativeAdjustment,
    ReportQuantitativeBaseline,
    UnresolvedWarning,
)

# ---------------------------------------------------------------------------
# Fixtures
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
        environment=DataEnvironment.REAL.value,
        publication_timestamp_trusted=False,
    )
    db_session.add(src)
    db_session.flush()
    return src


def _make_prediction_context(
    player_id: int = 1,
    gameweek: int = 1,
    **overrides: Any,
) -> PredictionContext:
    defaults = dict(
        player_id=player_id,
        gameweek=gameweek,
        expected_points=5.5,
        expected_minutes=60.0,
        start_probability=0.8,
        floor=2.0,
        ceiling=10.0,
        model_confidence=0.9,
        fixture_count=1,
        subject_ref=f"player:{player_id}",
        display_name="Test Player",
    )
    defaults.update(overrides)
    return PredictionContext(**defaults)


def _make_evidence(
    evidence_ref: str = "ev_1",
    is_mock: bool = False,
    temporal_class: str = LedgerTemporalClass.PRE_DEADLINE,
    **overrides: Any,
) -> EvidenceCitation:
    defaults = dict(
        evidence_ref=evidence_ref,
        kind="availability",
        summary="Player is injured.",
        source_name="test_source",
        source_reliability="verified_journalist",
        confidence=0.9,
        available_at=_utc(datetime(2025, 8, 15, 10, 0, 0)),
        ingested_at=_utc(datetime(2025, 8, 15, 10, 5, 0)),
        temporal_class=temporal_class,
        direction="negative",
        subject_ref="player:1",
        source_quote="Player is injured.",
        is_mock=is_mock,
    )
    defaults.update(overrides)
    return EvidenceCitation(**defaults)


# ---------------------------------------------------------------------------
# PredictionContext
# ---------------------------------------------------------------------------


class TestPredictionContext:
    def test_minimal_construction(self):
        pc = PredictionContext(
            player_id=1,
            gameweek=3,
            expected_points=5.5,
            expected_minutes=60.0,
            start_probability=0.8,
            floor=2.0,
            ceiling=10.0,
        )
        assert pc.player_id == 1
        assert pc.gameweek == 3
        assert pc.expected_points == 5.5
        assert pc.subject_ref == ""
        assert pc.display_name is None
        assert pc.model_confidence == 1.0
        assert pc.fixture_count == 1

    def test_to_prompt_dict(self):
        pc = _make_prediction_context(player_id=42, display_name="Salah")
        d = pc.to_prompt_dict()
        assert d["player_id"] == 42
        assert d["subject_ref"] == "player:42"
        assert d["display_name"] == "Salah"
        assert d["expected_points"] == 5.5
        assert d["fixture_count"] == 1

    def test_defaults_subject_ref(self):
        pc = PredictionContext(
            player_id=7,
            gameweek=1,
            expected_points=3.0,
            expected_minutes=45.0,
            start_probability=0.5,
            floor=0.0,
            ceiling=8.0,
        )
        assert pc.subject_ref == ""
        d = pc.to_prompt_dict()
        assert d["subject_ref"] == "player:7"
        assert d["display_name"] == "player:7"

    def test_frozen(self):
        pc = _make_prediction_context()
        with pytest.raises((AttributeError, Exception)):
            pc.player_id = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# IntelligenceReport model
# ---------------------------------------------------------------------------


class TestIntelligenceReport:
    def _make_report(self) -> IntelligenceReport:
        return IntelligenceReport(
            schema_version="phase9.report.v1",
            task="transfer_recommendation",
            headline="Test headline",
            prediction_context=ReportQuantitativeBaseline(
                subject_ref="player:1",
                player_id=1,
                gameweek=1,
                expected_points=5.5,
                expected_minutes=60.0,
                start_probability=0.8,
                floor=2.0,
                ceiling=10.0,
                fixture_count=1,
                display_name="Test Player",
            ),
            qualitative_adjustment=ReportQualitativeAdjustment(
                direction="neutral",
                magnitude="none",
                cited_evidence_refs=[],
                rationale="No evidence.",
            ),
            net_assessment="Baseline stated above; no qualitative signal.",
            recommendation="no_recommendation",
            confidence=0.4,
            confidence_band=ReportConfidence.LOW,
            citations=[
                ReportEvidenceCitation(
                    evidence_ref="ev_1",
                    kind="availability",
                    subject_ref="player:1",
                    summary="Injured.",
                    source_name="test",
                    source_reliability="unverified",
                    confidence=0.9,
                    direction="negative",
                )
            ],
            caveats=["Mock provider output."],
            generated_at=_utc(datetime(2025, 8, 15, 12, 0, 0)),
            provider_name="mock",
            model_name="mock-deterministic-v1",
            is_mock=True,
            prompt_hash="abc123",
        )

    def test_construction(self):
        report = self._make_report()
        assert report.schema_version == "phase9.report.v1"
        assert report.task == "transfer_recommendation"
        assert report.is_mock is True

    def test_confidence_bounds_rejected(self):
        base = self._make_report()
        data = base.to_dict()
        data["confidence"] = 1.5
        with pytest.raises(ValidationError):
            IntelligenceReport(**data)

    def test_extra_field_rejected(self):
        base = self._make_report()
        data = base.to_dict()
        data["invented"] = "bad"
        with pytest.raises(ValidationError):
            IntelligenceReport(**data)

    def test_to_dict_roundtrip(self):
        report = self._make_report()
        d = report.to_dict()
        assert d["schema_version"] == "phase9.report.v1"
        assert d["task"] == "transfer_recommendation"
        assert d["prediction_context"]["player_id"] == 1
        assert d["qualitative_adjustment"]["direction"] == "neutral"
        assert d["is_mock"] is True
        assert len(d["citations"]) == 1
        assert d["citations"][0]["evidence_ref"] == "ev_1"

    def test_render_markdown_contains_key_sections(self):
        report = self._make_report()
        md = report.render_markdown()
        assert "# Test headline" in md
        assert "## Quantitative Baseline" in md
        assert "## Qualitative Assessment" in md
        assert "## Evidence Cited" in md
        assert "## Net Assessment" in md
        assert "## Caveats" in md

    def test_render_markdown_contains_baseline_values(self):
        report = self._make_report()
        md = report.render_markdown()
        assert "5.5" in md
        assert "player:1" in md
        assert "Test Player" in md

    def test_render_markdown_no_citations(self):
        base = self._make_report()
        empty = base.model_copy(update={"citations": []})
        md = empty.render_markdown()
        assert "## Evidence Cited" not in md

    def test_render_markdown_unresolved_warnings(self):
        base = self._make_report()
        with_warning = base.model_copy(
            update={
                "unresolved_warnings": [
                    UnresolvedWarning(
                        evidence_ref="ev_2",
                        kind="availability",
                        subject_hint="Unknown Player",
                        resolution_status="unresolved_player",
                        resolution_reason="no match found",
                    )
                ]
            }
        )
        md = with_warning.render_markdown()
        assert "## Unresolved Warnings" in md
        assert "ev_2" in md
        assert "Unknown Player" in md


# ---------------------------------------------------------------------------
# AIAnalyst.generate_report
# ---------------------------------------------------------------------------


class TestAIAnalystGenerateReport:
    def test_generate_report_basic(self):
        provider = MockLLMProvider()
        analyst = AIAnalyst(provider, allow_mock_evidence=False)
        prediction = _make_prediction_context()
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))

        report = analyst.generate_report(
            prediction=prediction,
            evidence=[],
            deadline=deadline,
        )

        assert isinstance(report, IntelligenceReport)
        assert report.schema_version == "phase9.report.v1"
        assert report.task == "transfer_recommendation"
        assert report.prediction_context.player_id == 1
        assert report.prediction_context.expected_points == 5.5
        assert report.is_mock is True
        assert report.qualitative_adjustment.direction == "neutral"
        assert report.qualitative_adjustment.magnitude == "none"

    def test_generate_report_with_evidence(self):
        provider = MockLLMProvider()
        analyst = AIAnalyst(provider, allow_mock_evidence=False)
        prediction = _make_prediction_context()
        evidence = [_make_evidence()]
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))

        report = analyst.generate_report(
            prediction=prediction,
            evidence=evidence,
            deadline=deadline,
        )

        assert report.citations[0].evidence_ref == "ev_1"
        assert report.citations[0].kind == "availability"
        assert len(report.citations) == 1

    def test_generate_report_captaincy(self):
        provider = MockLLMProvider()
        analyst = AIAnalyst(provider, allow_mock_evidence=False)
        predictions = [
            _make_prediction_context(player_id=1, display_name="Salah"),
            _make_prediction_context(player_id=2, display_name="Haaland"),
        ]
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))

        report = analyst.captaincy_report(
            predictions=predictions,
            evidence=[],
            deadline=deadline,
        )

        assert report.task == "captaincy_debate"
        assert "Salah" in report.headline or "vs" in report.headline

    def test_captaincy_report_requires_two_candidates(self):
        provider = MockLLMProvider()
        analyst = AIAnalyst(provider, allow_mock_evidence=False)
        prediction = _make_prediction_context()
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        with pytest.raises(ValueError, match="at least two"):
            analyst.captaincy_report(
                predictions=[prediction],
                evidence=[],
                deadline=deadline,
            )

    def test_generate_report_differential(self):
        provider = MockLLMProvider()
        analyst = AIAnalyst(provider, allow_mock_evidence=False)
        prediction = _make_prediction_context()
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))

        report = analyst.differential_report(
            prediction=prediction,
            evidence=[],
            deadline=deadline,
        )

        assert report.task == "differential_risk"

    def test_generate_report_restates_baseline(self):
        provider = MockLLMProvider()
        analyst = AIAnalyst(provider, allow_mock_evidence=False)
        prediction = _make_prediction_context(expected_points=7.3)
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))

        report = analyst.generate_report(
            prediction=prediction,
            evidence=[],
            deadline=deadline,
        )

        pc = report.prediction_context
        assert pc.expected_points == 7.3
        assert pc.expected_minutes == 60.0
        assert pc.start_probability == 0.8

    def test_generate_report_guardrail_rejects_altered_baseline(self):
        """The analyst's guardrails must fire even when generating IntelligenceReport."""

        class BadProvider(MockLLMProvider):
            def _generate_analyst(self, prompt: Any) -> str:
                return json.dumps(
                    {
                        "schema_version": "phase9.analyst.v1",
                        "task": "transfer_recommendation",
                        "headline": "Bad",
                        "quantitative_baseline": [
                            {
                                "subject_ref": "player:1",
                                "expected_points": 99.9,
                                "start_probability": 0.8,
                                "floor": 2.0,
                                "ceiling": 10.0,
                                "interpretation": "wrong",
                            }
                        ],
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
                    }
                )

        analyst = AIAnalyst(BadProvider(), allow_mock_evidence=False)
        prediction = _make_prediction_context(expected_points=5.5)
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))

        with pytest.raises(AnalystGuardrailError, match="altered the quantitative baseline"):
            analyst.generate_report(
                prediction=prediction,
                evidence=[],
                deadline=deadline,
            )


# ---------------------------------------------------------------------------
# ReportConfidence helper
# ---------------------------------------------------------------------------


class TestReportConfidence:
    def test_confidence_band_thresholds(self):
        provider = MockLLMProvider()
        analyst = AIAnalyst(provider, allow_mock_evidence=False)

        # The mock provider returns confidence 0.4 when no evidence
        prediction = _make_prediction_context()
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        report = analyst.generate_report(
            prediction=prediction,
            evidence=[],
            deadline=deadline,
        )
        assert report.confidence_band == ReportConfidence.LOW

    def test_confidence_band_moderate(self):
        provider = MockLLMProvider()
        analyst = AIAnalyst(provider, allow_mock_evidence=False)
        prediction = _make_prediction_context()
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))
        evidence = [_make_evidence()]

        report = analyst.generate_report(
            prediction=prediction,
            evidence=evidence,
            deadline=deadline,
        )
        # Mock returns confidence 0.6 with evidence -> MODERATE
        assert report.confidence_band == ReportConfidence.MODERATE


# ---------------------------------------------------------------------------
# UnresolvedWarning flow through generate_report
# ---------------------------------------------------------------------------


class TestUnresolvedWarnings:
    def test_unresolved_evidence_collected_from_context(self):
        """When evidence is excluded (mock or post-deadline), it appears in
        unresolved_warnings on the report."""
        provider = MockLLMProvider()
        analyst = AIAnalyst(
            provider,
            allow_mock_evidence=False,
            strict_leakage=False,
        )
        prediction = _make_prediction_context()
        evidence = [_make_evidence(is_mock=True)]
        deadline = _utc(datetime(2025, 8, 17, 18, 30, 0))

        report = analyst.generate_report(
            prediction=prediction,
            evidence=evidence,
            deadline=deadline,
        )

        assert len(report.unresolved_warnings) == 1
        assert report.unresolved_warnings[0].evidence_ref == "ev_1"
        assert "mock" in report.unresolved_warnings[0].resolution_reason.lower()
