"""Phase 10.x — Admin / scheduler HTTP endpoints for FaaS (Vercel Cron).

Vercel cannot run the long-lived ``worker`` / ``bot`` PaaS processes, so the
periodic jobs are exposed as HTTP endpoints triggered by Vercel Cron (or by a
manual curl). Both GET and POST are accepted: Vercel Cron issues GET requests.

Auth: when ``CRON_SECRET`` is configured, requests must carry either the
``Authorization: Bearer <CRON_SECRET>`` header (which Vercel Cron sends
automatically) or a ``?secret=<CRON_SECRET>`` query parameter for manual use.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select

from fpl_intelligence.config import get_settings
from fpl_intelligence.collectors.official_fpl import OfficialFPLDataProvider
from fpl_intelligence.data_providers.api_football import ApiFootballConnector
from fpl_intelligence.data_providers.football_data_org import FootballDataOrgConnector
from fpl_intelligence.db.models import IngestionRun, RawRecord, Season
from fpl_intelligence.db.session import SessionLocal
from fpl_intelligence.ingestion.fpl import ingest_bootstrap, ingest_fixtures
from fpl_intelligence.live_intelligence.connectors import (
    FPLAPIConnector,
    RSSConnector,
)
from fpl_intelligence.live_intelligence.connectors.base import SourceConnector
from fpl_intelligence.live_intelligence.raw_item_ledger import (
    RawItem,
    ingest_raw_text,
)
from fpl_intelligence.live_intelligence.scheduling.alerts import AlertGenerator
from fpl_intelligence.live_intelligence.scheduling.scheduler import Scheduler

logger = logging.getLogger(__name__)

router = APIRouter()

SEASON_CODE = "2026-27"

#: Phase 11.1 fallback providers used when the official FPL API blocks Vercel's
#: datacenter IPs (403/429) or is unreachable.
_FALLBACK_WARNING = (
    "FPL API blocked (403). Falling back to API-Football for lineups and news."
)

#: FPL's API rejects non-browser User-Agents (403). Reuse a browser-like UA for
#: the live-intelligence scheduler connector too.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _require_cron_auth(
    authorization: Annotated[str | None, Header()] = None,
    secret: Annotated[str | None, Query()] = None,
) -> None:
    """Reject requests that cannot prove they are cron-originated."""
    expected = os.environ.get("CRON_SECRET")
    if not expected:
        # No secret configured: open (dev convenience). Set CRON_SECRET in prod.
        return
    ok = authorization == f"Bearer {expected}" or secret == expected
    if not ok:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _build_connectors() -> dict[str, SourceConnector]:
    connectors: dict[str, SourceConnector] = {
        "fpl_api": FPLAPIConnector(headers=_BROWSER_HEADERS)
    }
    rss_url = os.environ.get("RSS_FEED_URL")
    if rss_url:
        connectors["rss"] = RSSConnector(rss_url)
    return connectors


def _make_ingest_sink() -> Callable[..., None]:
    def sink(raw: RawItem, *, connector: SourceConnector, dry_run: bool) -> None:
        db = SessionLocal()
        try:
            ingest_raw_text(
                db,
                source_id=raw.source_id,
                text=raw.content_text,
                published_at=raw.published_at,
                url=getattr(raw, "url", None),
                external_id=raw.external_id,
                title=raw.title,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - isolate per-item failures
            logger.exception("ingest failed for %s: %s", raw.external_id, exc)
        finally:
            db.close()

    return sink


def _is_fpl_blocked(exc: BaseException) -> bool:
    """True when ``exc`` looks like an FPL block (403/429) or a connection error.

    Vercel's shared egress IPs are intermittently 403/429'd by the official FPL
    API, and datacenter IP ranges can also be unreachable. Both cases must
    trigger the Phase 11.1 fallback rather than failing the ingest.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (403, 429)
    # httpx.HTTPError covers ConnectError, timeouts, TransportError, etc.
    if isinstance(exc, httpx.HTTPError):
        return True
    return False


def _hash_fallback_payload(payload: object) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(serialized).hexdigest()


def _get_or_create_season(db, code: str) -> Season:
    season = db.scalar(select(Season).where(Season.code == code))
    if season:
        return season
    season = Season(code=code, display_name=code.replace("-", "/"))
    db.add(season)
    db.flush()
    return season


def _save_fallback_raw_record(
    db, source: str, endpoint: str, payload: object, season_code: str
) -> int:
    """Insert a RawRecord for a fallback provider, skipping duplicate payloads."""
    payload_hash = _hash_fallback_payload(payload)
    existing = db.scalar(
        select(RawRecord).where(
            RawRecord.source == source,
            RawRecord.endpoint == endpoint,
            RawRecord.payload_hash == payload_hash,
        )
    )
    if existing is not None:
        return 0
    db.add(
        RawRecord(
            source=source,
            provider=source,
            endpoint=endpoint,
            retrieved_at=datetime.now(UTC),
            payload_hash=payload_hash,
            payload=dict(payload),
            season_code=season_code,
        )
    )
    db.flush()
    return 1


def _run_fpl_ingest_fallback(
    db,
    season_code: str,
    *,
    api_football: ApiFootballConnector | None = None,
    football_data: FootballDataOrgConnector | None = None,
) -> dict:
    """Populate the DB from the Phase 11.1 providers when official FPL is blocked.

    Writes the fetched fixtures / lineups / injuries / matches into ``RawRecord``
    so the database is never left empty, and records an ``IngestionRun`` for
    observability. Each provider is isolated: a failure in one never aborts the
    others.
    """
    api_football = api_football or ApiFootballConnector()
    football_data = football_data or FootballDataOrgConnector()
    started = datetime.now(UTC)
    run = IngestionRun(
        source="api_football+football_data_org",
        job_name="fallback",
        season_code=season_code,
        status="RUNNING",
        started_at=started,
    )
    db.add(run)
    db.flush()
    _get_or_create_season(db, season_code)

    records = 0
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    if api_football.is_enabled():
        try:
            fixtures = api_football.fetch_fixtures_by_date(today)
            records += _save_fallback_raw_record(
                db, "api_football", f"/fixtures?date={today}", {"response": fixtures}, season_code
            )
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning("API-Football fixtures fetch failed during fallback.", exc_info=True)
        try:
            facts = api_football.collect_player_facts(date=today)
            if facts:
                records += _save_fallback_raw_record(
                    db,
                    "api_football",
                    f"/player-facts?date={today}",
                    [f.to_dict() for f in facts],
                    season_code,
                )
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning("API-Football player-facts fetch failed during fallback.", exc_info=True)

    if football_data.is_enabled():
        try:
            competitions = football_data.fetch_competitions()
            records += _save_fallback_raw_record(
                db,
                "football_data_org",
                "/competitions",
                {"competitions": [c.to_dict() for c in competitions]},
                season_code,
            )
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning("football-data.org competitions fetch failed during fallback.", exc_info=True)
        try:
            matches = football_data.fetch_matches()
            records += _save_fallback_raw_record(
                db,
                "football_data_org",
                "/matches",
                {"matches": [m.to_dict() for m in matches]},
                season_code,
            )
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning("football-data.org matches fetch failed during fallback.", exc_info=True)

    run.status = "SUCCESS"
    run.records_processed = records
    run.finished_at = datetime.now(UTC)
    db.commit()
    return {"records": records}


def _run_scheduler_fallback(sink: Callable[..., None]) -> int:
    """Feed API-Football / football-data.org items through the scheduler sink.

    Used when the official FPL connector is blocked so the live-intelligence
    pipeline still ingests lineups / news / matches from the fallback providers.
    """
    ingested = 0
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    api_football = ApiFootballConnector()
    if api_football.is_enabled():
        try:
            for fact in api_football.collect_player_facts(date=today):
                raw = RawItem.create(
                    source_id=f"api_football:{fact.api_football_player_id}",
                    title=f"{fact.name} ({fact.status})",
                    content_text=json.dumps(fact.to_dict(), default=str),
                    published_at=datetime.now(UTC),
                    scraped_at=datetime.now(UTC),
                    ingested_at=datetime.now(UTC),
                    url=None,
                    external_id=f"api_football:{fact.api_football_player_id}:{today}",
                )
                sink(raw, connector=api_football, dry_run=False)
                ingested += 1
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning("API-Football fallback fetch failed during scheduler pass.", exc_info=True)
    football_data = FootballDataOrgConnector()
    if football_data.is_enabled():
        try:
            for match in football_data.fetch_matches():
                raw = RawItem.create(
                    source_id=f"football_data_org:match:{match.id}",
                    title=f"{match.home_team} vs {match.away_team}",
                    content_text=json.dumps(match.to_dict(), default=str),
                    published_at=datetime.now(UTC),
                    scraped_at=datetime.now(UTC),
                    ingested_at=datetime.now(UTC),
                    url=None,
                    external_id=f"football_data_org:match:{match.id}",
                )
                sink(raw, connector=football_data, dry_run=False)
                ingested += 1
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning("football-data.org fallback fetch failed during scheduler pass.", exc_info=True)
    return ingested


def _run_scheduler_pass() -> dict:
    connectors = _build_connectors()
    scheduler = Scheduler(
        connectors,
        ingest=_make_ingest_sink(),
        alert_generator=AlertGenerator(),
        min_interval_seconds=0.0,
    )
    report = scheduler.run()
    return report.to_dict()


def _run_fpl_ingest() -> dict:
    settings = get_settings()
    provider = OfficialFPLDataProvider(
        base_url=settings.fpl_base_url,
        timeout=settings.request_timeout_seconds,
        max_retries=settings.max_retries,
    )
    db = SessionLocal()
    try:
        try:
            bootstrap = ingest_bootstrap(db, provider, SEASON_CODE)
            fixtures = ingest_fixtures(db, provider, SEASON_CODE)
            return {"bootstrap": bootstrap, "fixtures": fixtures, "fallback": False}
        except Exception as exc:  # noqa: BLE001 - decide block vs hard failure
            if not _is_fpl_blocked(exc):
                raise
            logger.warning(_FALLBACK_WARNING)
            records = _run_fpl_ingest_fallback(db, SEASON_CODE)
            return {
                "fallback": True,
                "provider": "api_football+football_data_org",
                **records,
            }
    finally:
        db.close()


@router.get("/admin/run-scheduler")
@router.post("/admin/run-scheduler")
async def run_scheduler_endpoint(
    _: None = Depends(_require_cron_auth),
) -> dict:
    """Run one live-intelligence scheduler pass (fetch → ingest → alert).

    If the official FPL connector is blocked (403/429/unreachable) the pass
    still succeeds via the other connectors, and API-Football / football-data.org
    items are additionally ingested through the fallback path.
    """
    try:
        report = await run_in_threadpool(_run_scheduler_pass)
    except Exception as exc:  # noqa: BLE001 - surface the error for cron visibility
        logger.exception("scheduler pass failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )

    fpl_blocked = bool(
        report.get("connector_report", {}).get("connectors", {}).get("fpl_api", {}).get("errors")
    )
    if fpl_blocked:
        logger.warning(_FALLBACK_WARNING)
        fallback_items = _run_scheduler_fallback(_make_ingest_sink())
        report["fpl_blocked"] = True
        report["fallback_items"] = fallback_items
    return report


@router.get("/admin/ingest-fpl")
@router.post("/admin/ingest-fpl")
async def ingest_fpl_endpoint(
    _: None = Depends(_require_cron_auth),
) -> dict:
    """Ingest the official FPL bootstrap + fixtures for the season."""
    try:
        return await run_in_threadpool(_run_fpl_ingest)
    except Exception as exc:  # noqa: BLE001 - surface the error for cron visibility
        logger.exception("fpl ingest failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
