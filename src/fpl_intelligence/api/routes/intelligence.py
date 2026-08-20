"""Phase 10.1 — Intelligence REST endpoints.

Exposes the Phase 9 Live Intelligence Accumulator and AI Analyst over HTTP so
that dashboards, bots and mobile apps can consume the data without touching the
quantitative Phases 1–8 stack.

Four endpoints are provided:

* ``GET  /api/v1/health`` — Phase 9.8 deployment health status + metrics.
* ``GET  /api/v1/intelligence/player/{player_id}`` — an :class:`IntelligenceReport`
  for one player, built by the :class:`AnalystReportGenerator` (Phase 9.4).
* ``POST /api/v1/ingest`` — feed raw text through the Phase 9.2
  ``ingest_raw_text`` pipeline.
* ``GET  /api/v1/intelligence/unresolved`` — a paginated list of
  :class:`UnresolvedLiveEvidence` (Phase 9.2.1) for human triage.

LLM safety
----------
Every blocking call (LLM synthesis, DB ingestion) runs through
:func:`fastapi.concurrency.run_in_threadpool` so it never pins the event loop.
The LLM provider injected by :func:`deps.get_llm_provider` defaults to the
deterministic :class:`MockLLMProvider`; a real provider is only built when the
caller opts in via the ``FPL_API_USE_LIVE_LLM`` env var or the
``X-FPL-LLM-Mode: live`` header. No API keys are hardcoded.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fpl_intelligence import __version__
from fpl_intelligence.api import deps
from fpl_intelligence.deployment.monitoring import (
    MetricKind,
    build_monitoring_service,
)
from fpl_intelligence.live_intelligence.analyst import AnalystTask
from fpl_intelligence.live_intelligence.bridge import AnalystReportGenerator
from fpl_intelligence.live_intelligence.extraction import LLMProvider
from fpl_intelligence.live_intelligence.models import UnresolvedLiveEvidence
from fpl_intelligence.live_intelligence.raw_item_ledger import (
    ManualIngestReport,
    ingest_raw_text,
)
from fpl_intelligence.live_intelligence.report import IntelligenceReport

router = APIRouter()

#: Phase 9.8 production-deployment tag this API fronts (Phase 9 is closed).
_DEPLOYMENT_TAG = "v0.9.8-production-deployment"

#: Shared monitoring singleton (Phase 9.8 metrics + health registries).
_MONITORING = build_monitoring_service(
    type(
        "Cfg",
        (),
        {
            "metrics_enabled": True,
            "critical_error_webhook_url": os.getenv("FPL_CRITICAL_ERROR_WEBHOOK_URL"),
            "health_check_interval_seconds": 60,
        },
    )()
)


def _probe_database(db: Session) -> tuple[bool, str]:
    """Probe the injected DB session for connectivity without modifying anything.

    Returns ``(ok, detail)`` where ``ok`` is True when ``SELECT 1`` succeeds and
    ``detail`` is a human-readable status string. Any failure (connection down,
    pool exhausted, credentials rejected) is caught and surfaced as ``(False, ...)``
    so the health check never crashes.
    """
    try:
        db.execute(select(1)).scalar_one()
    except Exception as exc:  # noqa: BLE001 - report connectivity, don't crash health
        return False, str(exc)
    return True, "postgres/sqlite reachable"


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, assuming UTC for naive inputs."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    """Payload for ``POST /api/v1/ingest``."""

    source_id: str = Field(..., description="Phase 9.2 source identifier.")
    content_text: str = Field(..., description="Raw unstructured content.")
    published_at: str = Field(
        ..., description="ISO-8601 publication time (timezone-aware)."
    )
    url: str | None = Field(None, description="Optional source URL.")
    external_id: str | None = Field(None, description="Optional provider-side id.")
    title: str | None = Field(None, description="Optional display title.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unresolved_to_dict(row: UnresolvedLiveEvidence) -> dict[str, Any]:
    """Project one :class:`UnresolvedLiveEvidence` row into a JSON-safe dict."""
    return {
        "id": row.id,
        "raw_item_id": row.raw_item_id,
        "source_id": row.source_id,
        "extraction_run_id": row.extraction_run_id,
        "evidence_type": row.evidence_type,
        "player_name": row.player_name,
        "team_name": row.team_name,
        "team_hint": row.team_hint,
        "status_mentioned": row.status_mentioned,
        "quote": row.quote,
        "confidence": row.confidence,
        "resolution_status": row.resolution_status,
        "resolution_reason": row.resolution_reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/health")
async def health(db: deps.GetDB) -> dict[str, Any]:
    """Phase 9.8 deployment health status and metrics.

    Probes database connectivity (without modifying anything) and surfaces the
    shared Phase 9.8 :class:`MonitoringService` metric/health snapshot.
    """
    db_ok, db_detail = _probe_database(db)

    _MONITORING.report_health("database", db_ok, db_detail)
    _MONITORING.report_health("intelligence_api", True, "API router reachable")
    _MONITORING.record_metric("api_health_checks_total", 1.0, kind=MetricKind.COUNTER)

    snapshot = _MONITORING.snapshot()
    overall = "ok" if _MONITORING.health.all_ok() else "degraded"

    return {
        "status": overall,
        "version": __version__,
        "phase": "10.1",
        "deployment_tag": _DEPLOYMENT_TAG,
        "phase9_8_deployment": {
            "tag": _DEPLOYMENT_TAG,
            "status": "closed",
        },
        "database": {
            "ok": db_ok,
            "status": "up" if db_ok else "down",
            "detail": db_detail,
        },
        "api": {
            "ok": True,
            "status": "up",
        },
        "monitoring": snapshot,
    }


@router.get("/intelligence/player/{player_id}")
async def player_intelligence(
    player_id: int,
    gameweek: int | None = Query(None, description="FPL gameweek number (default 1)."),
    cutoff: str | None = Query(None, description="ISO-8601 cutoff; defaults to now."),
    format: str | None = Query(None, description="Response format: 'md' for Markdown."),
    accept: str | None = Header(default=None, description="e.g. 'text/markdown'."),
    builder: deps.PredictionContextBuilder = Depends(deps.get_prediction_builder),  # noqa: B008
    evidence_service: deps.EvidenceQueryService = Depends(deps.get_evidence_service),  # noqa: B008
    analyst: deps.AIAnalyst = Depends(deps.get_analyst),  # noqa: B008
) -> Any:
    """Generate an :class:`IntelligenceReport` for one player.

    Uses the Phase 9.4 :class:`AnalystReportGenerator`. Returns JSON by default
    or Markdown when ``?format=md`` or ``Accept: text/markdown`` is supplied.
    """
    gw = gameweek if gameweek is not None else 1
    cutoff_dt = _parse_iso(cutoff) if cutoff else datetime.now(UTC)

    generator = AnalystReportGenerator(
        builder,
        evidence_service,
        analyst.provider,
        task=AnalystTask.TRANSFER_RECOMMENDATION,
    )
    report: IntelligenceReport = await run_in_threadpool(
        generator.generate,
        player_id,
        gw,
        cutoff_time=cutoff_dt,
    )

    want_markdown = format == "md" or (accept is not None and "text/markdown" in accept)
    if want_markdown:
        return PlainTextResponse(report.render_markdown(), media_type="text/markdown")
    return report


@router.post("/ingest")
async def ingest(
    request: IngestRequest,
    db: deps.GetDB,
    provider: LLMProvider = Depends(deps.get_llm_provider),  # noqa: B008
) -> dict[str, Any]:
    """Ingest raw text through the Phase 9.2 ``ingest_raw_text`` pipeline.

    Returns the ingestion summary, the raw-item id, the extraction run id, and
    the persisted availability / tactical / unresolved evidence ids.
    """
    try:
        published_at = _parse_iso(request.published_at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid published_at: {exc}") from exc

    result: ManualIngestReport = await run_in_threadpool(
        ingest_raw_text,
        db,
        source_id=request.source_id,
        text=request.content_text,
        published_at=published_at,
        url=request.url,
        external_id=request.external_id,
        title=request.title,
        provider=provider,
    )
    return result.to_dict()


@router.get("/intelligence/unresolved")
async def unresolved(
    db: deps.GetDB,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Paginated list of :class:`UnresolvedLiveEvidence` for human triage."""
    total_rows = db.scalar(
        select(func.count()).select_from(UnresolvedLiveEvidence)
    ) or 0

    stmt = (
        select(UnresolvedLiveEvidence)
        .order_by(UnresolvedLiveEvidence.id)
        .limit(limit)
        .offset(offset)
    )
    rows = db.scalars(stmt).all()

    _MONITORING.record_metric("api_unresolved_reviewed_total", 1.0, kind=MetricKind.COUNTER)

    return {
        "items": [_unresolved_to_dict(row) for row in rows],
        "total": total_rows,
        "limit": limit,
        "offset": offset,
    }
