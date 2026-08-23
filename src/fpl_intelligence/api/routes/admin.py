"""Phase 10.x — Admin / scheduler HTTP endpoints for FaaS (Vercel Cron).

Vercel cannot run the long-lived ``worker`` / ``bot`` PaaS processes, so the
periodic jobs are exposed as HTTP endpoints triggered by Vercel Cron (or by a
manual curl). Both GET and POST are accepted: Vercel Cron issues GET requests.

Auth: when ``CRON_SECRET`` is configured, requests must carry the
``Authorization: Bearer <CRON_SECRET>`` header (which Vercel Cron sends
automatically).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from fpl_intelligence.collectors.official_fpl import OfficialFPLDataProvider
from fpl_intelligence.config import get_settings
from fpl_intelligence.data_providers.api_football import ApiFootballConnector
from fpl_intelligence.data_providers.football_data_org import FootballDataOrgConnector
from fpl_intelligence.db.models import (
    Gameweek,
    IngestionRun,
    Player,
    PlayerExternalId,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
    RawRecord,
    Season,
)
from fpl_intelligence.db.session import SessionLocal
from fpl_intelligence.ingestion.fpl import (
    _get_or_create_player,
    _get_or_create_team,
    ingest_bootstrap,
    ingest_fixtures,
)
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
from fpl_intelligence.squad.sync import NoPendingSync, run_pending_sync

logger = logging.getLogger(__name__)

router = APIRouter()

SEASON_CODE = "2026-27"

#: Phase 11.1 fallback providers used when the official FPL API blocks Vercel's
#: datacenter IPs (403/429) or is unreachable.
_FALLBACK_WARNING = "FPL API blocked (403). Falling back to API-Football for lineups and news."

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
) -> None:
    """Reject requests that cannot prove they are cron-originated.

    Standardized on ``Authorization: Bearer <CRON_SECRET>`` — the single auth
    mechanism that Vercel Cron sends automatically. The legacy ``?secret=``
    query parameter is no longer accepted.
    """
    expected = os.environ.get("CRON_SECRET")
    if not expected:
        # No secret configured: open (dev convenience). Set CRON_SECRET in prod.
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def _build_connectors() -> dict[str, SourceConnector]:
    connectors: dict[str, SourceConnector] = {"fpl_api": FPLAPIConnector(headers=_BROWSER_HEADERS)}
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
    return isinstance(exc, httpx.HTTPError)


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
        except Exception:  # noqa: BLE001 - degrade gracefully
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
        except Exception:  # noqa: BLE001 - degrade gracefully
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
        except Exception:  # noqa: BLE001 - degrade gracefully
            logger.warning(
                "football-data.org competitions fetch failed during fallback.",
                exc_info=True,
            )
        try:
            matches = football_data.fetch_matches()
            records += _save_fallback_raw_record(
                db,
                "football_data_org",
                "/matches",
                {"matches": [m.to_dict() for m in matches]},
                season_code,
            )
        except Exception:  # noqa: BLE001 - degrade gracefully
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
        except Exception:  # noqa: BLE001 - degrade gracefully
            logger.warning(
                "API-Football fallback fetch failed during scheduler pass.",
                exc_info=True,
            )
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
        except Exception:  # noqa: BLE001 - degrade gracefully
            logger.warning(
                "football-data.org fallback fetch failed during scheduler pass.",
                exc_info=True,
            )
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

    # Phase 13.5 — fold squad auto-sync into the existing daily cron. The
    # run-scheduler cron already carries the Vercel cron Bearer token, so this
    # needs no new cron slot, no GitHub Actions, and no new secrets.
    report["auto_sync"] = await _run_pending_sync_in_scheduler()
    return report


async def _run_pending_sync_in_scheduler() -> dict:
    """Retry any queued squad auto-sync during the daily scheduler pass.

    Never raises: the scheduler must complete even if the sync (or the
    Telegram push) fails. Returns a small report of what happened.
    """
    db = SessionLocal()
    try:
        await run_pending_sync(db)
        return {"queued": True, "synced": True}
    except NoPendingSync:
        return {"queued": False, "synced": False}
    except Exception as exc:  # noqa: BLE001 - scheduler must never fail on sync
        logger.warning("Pending squad sync during scheduler failed: %s", exc)
        return {"queued": True, "synced": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()


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


# --------------------------------------------------------------------------- #
# Hotfix v1.3.4 — offline bootstrap seed
# --------------------------------------------------------------------------- #
# FPL blocks Vercel's datacenter IPs, so the live /api/v1/admin/ingest-fpl path
# can never populate real teams + prices on the deployed PostgreSQL. The fix
# commits a minimal offline snapshot (data/seed/fpl_bootstrap_seed.json, fetched
# once from a non-blocked machine) and replays it here so PlayerTeamMembership +
# PlayerGameweekPerformance get real values even though the live API is blocked.
# --------------------------------------------------------------------------- #

_SEED_REL = Path("data") / "seed" / "fpl_bootstrap_seed.json"


def _resolve_seed_path() -> Path:
    """Locate the committed seed file from the repo root *or* the Vercel bundle.

    ``vercel.json`` ships ``data/seed/**`` inside the function bundle (see
    includeFiles), so both the package-relative probe and the cwd / /var/task
    probes resolve to the same committed file.
    """
    candidates = [
        Path(__file__).resolve().parents[4] / _SEED_REL,
        Path.cwd() / _SEED_REL,
        Path("/var/task") / _SEED_REL,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "fpl_bootstrap_seed.json not found; looked in " + ", ".join(str(c) for c in candidates)
    )


def _seed_from_file(db: Session, path: Path) -> dict:
    """Populate PlayerTeamMembership + PlayerGameweekPerformance from the seed.

    Idempotent: existing memberships / price snapshots (matched by
    player+team+season and player+gameweek) are left untouched, so re-running
    reports zero new rows instead of duplicating data.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    meta = payload.get("meta", {}) or {}
    season_code = str(meta.get("season_code") or SEASON_CODE)
    teams_raw = payload.get("teams", []) or []
    events_raw = payload.get("events", []) or []
    players_raw = payload.get("players", []) or []

    season = _get_or_create_season(db, season_code)

    team_ext_map: dict[str, int] = {}
    for item in teams_raw:
        provider_id = str(int(item["id"]))
        team = _get_or_create_team(
            db,
            provider_id,
            str(item.get("name", "Unknown")),
            str(item.get("short_name", "") or item.get("name", "")[:3].upper()) or None,
        )
        team_ext_map[provider_id] = team.id

    # Reference gameweek (lowest provider_event_id) carries the price snapshot
    # so GET /api/v1/players can return non-null price via PlayerGameweekPerformance.
    reference_gw = None
    for ev in events_raw:
        provider_event_id = int(ev["id"])
        gw = db.scalar(
            select(Gameweek).where(
                Gameweek.season_id == season.id,
                Gameweek.provider_event_id == provider_event_id,
            )
        )
        if gw is None:
            gw = Gameweek(
                season_id=season.id,
                provider_event_id=provider_event_id,
                name=str(ev.get("name", f"Gameweek {provider_event_id}")),
            )
            db.add(gw)
            db.flush()
        if reference_gw is None or gw.provider_event_id < reference_gw.provider_event_id:
            reference_gw = gw

    players_seeded = 0
    memberships_created = 0
    performances_created = 0
    for item in players_raw:
        provider_id = str(int(item["id"]))
        player = _get_or_create_player(
            db,
            provider_id,
            str(item.get("first_name", "")),
            str(item.get("second_name", "")),
            str(item.get("web_name", "")),
            int(item["position"]) if item.get("position") is not None else None,
            fpl_code=item.get("code"),
        )
        team_provider_id = str(int(item["team"])) if item.get("team") is not None else None
        team_id = team_ext_map.get(team_provider_id) if team_provider_id else None
        now_cost = item.get("now_cost")
        price = (float(now_cost) / 10.0) if now_cost is not None else None

        if team_id is not None:
            existing_membership = db.scalar(
                select(PlayerTeamMembership).where(
                    PlayerTeamMembership.player_id == player.id,
                    PlayerTeamMembership.team_id == team_id,
                    PlayerTeamMembership.season_id == season.id,
                )
            )
            if existing_membership is None:
                db.add(
                    PlayerTeamMembership(
                        player_id=player.id,
                        team_id=team_id,
                        season_id=season.id,
                        valid_from=season.start_date,
                    )
                )
                memberships_created += 1

            if reference_gw is not None:
                existing_pgp = db.scalar(
                    select(PlayerGameweekPerformance).where(
                        PlayerGameweekPerformance.player_id == player.id,
                        PlayerGameweekPerformance.gameweek_id == reference_gw.id,
                    )
                )
                if existing_pgp is None:
                    db.add(
                        PlayerGameweekPerformance(
                            player_id=player.id,
                            gameweek_id=reference_gw.id,
                            season_id=season.id,
                            team_id=team_id,
                            price=price,
                        )
                    )
                    performances_created += 1

        players_seeded += 1

    db.commit()
    return {
        "ok": True,
        "source": str(path),
        "season_code": season_code,
        "players": players_seeded,
        "memberships_created": memberships_created,
        "performances_created": performances_created,
    }


@router.get("/admin/seed-from-file")
@router.post("/admin/seed-from-file")
async def seed_from_file_endpoint(
    _: None = Depends(_require_cron_auth),
) -> dict:
    """Populate team memberships + price snapshots from the committed FPL seed.

    POST /api/v1/admin/seed-from-file with the
    ``Authorization: Bearer <CRON_SECRET>`` header. Safe to call repeatedly.
    """
    try:
        path = _resolve_seed_path()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    db = SessionLocal()
    try:
        return await run_in_threadpool(_seed_from_file, db, path)
    except Exception as exc:  # noqa: BLE001 - surface for visibility
        db.rollback()
        logger.exception("seed-from-file failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Phase 13.6 — one-time data initialization
# --------------------------------------------------------------------------- #
# Temporary, UNAUTHENTICATED bootstrap so the freshly-deployed PostgreSQL gets
# real teams + prices even though FPL blocks Vercel's datacenter IPs. Unlike
# the CRON_SECRET-protected seed-from-file endpoint, this endpoint is meant to
# be hit exactly once (from the deploy shell / dashboard). It seals itself
# after a successful run (returns 410 thereafter) so it never becomes a
# persistent unauthenticated write path.
# --------------------------------------------------------------------------- #

_INIT_JOB_NAME = "initialize-data"
_INIT_SOURCE = "seed"


def _initialization_complete(db: Session) -> bool:
    """True once a successful initialize-data run has been recorded."""
    return (
        db.scalar(
            select(IngestionRun).where(
                IngestionRun.job_name == _INIT_JOB_NAME,
                IngestionRun.source == _INIT_SOURCE,
                IngestionRun.status == "SUCCESS",
            )
        )
        is not None
    )


@router.post("/admin/initialize-data")
async def initialize_data_endpoint() -> dict:
    """Seed teams + prices from the committed FPL seed, exactly once.

    Returns 410 after the first successful run. Unauthenticated by design: it
    is a temporary, self-disabling one-shot bootstrap for a fresh deployment.
    """
    db = SessionLocal()
    try:
        if _initialization_complete(db):
            return JSONResponse(
                status_code=410,
                content={
                    "ok": False,
                    "error": "Data already initialized. This endpoint is disabled "
                    "after its first successful run.",
                },
            )
        try:
            path = _resolve_seed_path()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        report = await run_in_threadpool(_seed_from_file, db, path)

        # Seal the endpoint only after seeding fully succeeded.
        db.add(
            IngestionRun(
                source=_INIT_SOURCE,
                job_name=_INIT_JOB_NAME,
                season_code=str(report.get("season_code") or SEASON_CODE),
                status="SUCCESS",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                records_processed=int(report.get("players") or 0),
            )
        )
        db.commit()
        report["initialized"] = True
        return report
    except Exception as exc:  # noqa: BLE001 - surface for visibility
        db.rollback()
        logger.exception("initialize-data failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Phase 14.0 hotfix — apply pending schema migration to the deployed database
# --------------------------------------------------------------------------- #
# The deployed PostgreSQL predates migration 0015 (players.fpl_code), so every
# endpoint selecting that column returns HTTP 500. Vercel marks DATABASE_URL
# sensitive so it cannot be pulled into a local shell; therefore — mirroring
# the Phase 13.6 bootstrap — this is a narrow, UNAUTHENTICATED one-shot that:
#   1. adds players.fpl_code when missing,
#   2. stamps alembic_version to 0015 when the version table exists,
#   3. replays the idempotent seed so every player gets their FPL code.
# It seals itself after a successful run (returns 410 thereafter), exactly like
# initialize-data, so it never becomes a persistent unauthenticated write path.
# --------------------------------------------------------------------------- #

_MIGRATE_JOB_NAME = "migrate-fpl-code"


def _migration_applied(db: Session) -> bool:
    """True once a successful migrate-fpl-code run has been recorded."""
    return (
        db.scalar(
            select(IngestionRun).where(
                IngestionRun.job_name == _MIGRATE_JOB_NAME,
                IngestionRun.source == _INIT_SOURCE,
                IngestionRun.status == "SUCCESS",
            )
        )
        is not None
    )


@router.post("/admin/migrate-fpl-code")
async def migrate_fpl_code_endpoint() -> dict:
    """Add ``players.fpl_code`` and backfill it from the seed, exactly once.

    Returns 410 after the first successful run. Unauthenticated by design: it
    is a temporary, self-disabling one-shot migration for the fresh deployment.
    """
    db = SessionLocal()
    try:
        if _migration_applied(db):
            return JSONResponse(
                status_code=410,
                content={
                    "ok": False,
                    "error": "Migration already applied. This endpoint is disabled "
                    "after its first successful run.",
                },
            )

        insp = sa_inspect(db.get_bind())
        column_added = False
        if "fpl_code" not in [c["name"] for c in insp.get_columns("players")]:
            db.execute(text("ALTER TABLE players ADD COLUMN fpl_code INTEGER"))
            column_added = True
        if insp.has_table("alembic_version"):
            db.execute(text("DELETE FROM alembic_version"))
            db.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0015')"))
        db.commit()

        report = await run_in_threadpool(_seed_from_file, db, _resolve_seed_path())

        total = db.scalar(select(func.count()).select_from(Player)) or 0
        coded = (
            db.scalar(select(func.count()).select_from(Player).where(Player.fpl_code.is_not(None)))
            or 0
        )
        db.add(
            IngestionRun(
                source=_INIT_SOURCE,
                job_name=_MIGRATE_JOB_NAME,
                season_code=str(report.get("season_code") or SEASON_CODE),
                status="SUCCESS",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                records_processed=int(coded),
            )
        )
        db.commit()
        return {
            "ok": True,
            "column_added": column_added,
            "players": int(total),
            "players_with_code": int(coded),
            "seed_report": report,
        }
    except Exception as exc:  # noqa: BLE001 - surface for visibility
        db.rollback()
        logger.exception("migrate-fpl-code failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# TEMPORARY: Alembic version check endpoint (for debugging migration 0016)
# --------------------------------------------------------------------------- #


@router.get("/admin/alembic-version")
async def alembic_version_check() -> dict:
    """Report the current alembic migration version and column presence.

    TEMPORARY diagnostic endpoint — remove after migration is confirmed.
    """
    db = SessionLocal()
    try:
        insp = sa_inspect(db.get_bind())
        columns = [c["name"] for c in insp.get_columns("players")]
        has_fpl_element_id = "fpl_element_id" in columns
        has_fpl_code = "fpl_code" in columns

        version = None
        if insp.has_table("alembic_version"):
            row = db.execute(text("SELECT version_num FROM alembic_version")).first()
            version = row[0] if row else None

        return {
            "ok": True,
            "alembic_version": version,
            "expected_version": "0016_player_fpl_element_id",
            "behind": version != "0016",
            "columns": {
                "fpl_code": has_fpl_code,
                "fpl_element_id": has_fpl_element_id,
            },
        }
    except Exception as exc:  # noqa: BLE001 - surface for visibility
        logger.exception("alembic-version check failed")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"{type(exc).__name__}: {exc}"},
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Squad imports store OFFICIAL FPL element ids as player_ids, and the decisions
# enrichment now joins them via ``players.fpl_element_id`` (migration 0016).
# Vercel's build command is just ``pip install .`` — migrations never run on
# deploy — so the deployed database must be patched out-of-band, exactly like
# the Phase 14.0 fpl_code hotfix above. This narrow, UNAUTHENTICATED one-shot:
#   1. adds players.fpl_element_id (+ unique index) when missing,
#   2. backfills it from the official_fpl external-id mapping,
#   3. stamps alembic_version to 0016 when the version table exists,
#   4. replays the idempotent seed so every row carries its real element id.
# It seals itself after a successful run (410 thereafter).
# --------------------------------------------------------------------------- #

_MIGRATE_ELEMENT_JOB_NAME = "migrate-fpl-element-id"


def _element_migration_applied(db: Session) -> bool:
    """True once a successful migrate-fpl-element-id run has been recorded."""
    return (
        db.scalar(
            select(IngestionRun).where(
                IngestionRun.job_name == _MIGRATE_ELEMENT_JOB_NAME,
                IngestionRun.source == _INIT_SOURCE,
                IngestionRun.status == "SUCCESS",
            )
        )
        is not None
    )


@router.post("/admin/migrate-fpl-element-id")
async def migrate_fpl_element_id_endpoint() -> dict:
    """Add ``players.fpl_element_id`` and backfill it, exactly once.

    Returns 410 after the first successful run. Unauthenticated by design: it
    is a temporary, self-disabling one-shot migration for the deployment.
    """
    db = SessionLocal()
    try:
        if _element_migration_applied(db):
            return JSONResponse(
                status_code=410,
                content={
                    "ok": False,
                    "error": "Migration already applied. This endpoint is disabled "
                    "after its first successful run.",
                },
            )

        insp = sa_inspect(db.get_bind())
        columns = [c["name"] for c in insp.get_columns("players")]
        column_added = False
        if "fpl_element_id" not in columns:
            db.execute(text("ALTER TABLE players ADD COLUMN fpl_element_id INTEGER"))
            column_added = True
        indexes = [i["name"] for i in insp.get_indexes("players")]
        if "ix_players_fpl_element_id" not in indexes:
            db.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_players_fpl_element_id "
                    "ON players (fpl_element_id)"
                )
            )
        db.commit()

        # Backfill from the official FPL external-id mapping (pre-seed rows).
        ext_rows = db.execute(
            select(PlayerExternalId.player_id, PlayerExternalId.provider_player_id).where(
                PlayerExternalId.provider.in_(("official_fpl", "fpl"))
            )
        ).all()
        backfilled = 0
        for player_id, provider_player_id in ext_rows:
            if not str(provider_player_id).isdigit():
                continue
            player = db.get(Player, int(player_id))
            if player is not None and player.fpl_element_id is None:
                player.fpl_element_id = int(provider_player_id)
                backfilled += 1
        db.commit()

        if insp.has_table("alembic_version"):
            db.execute(text("DELETE FROM alembic_version"))
            db.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0016')"))
            db.commit()

        report = await run_in_threadpool(_seed_from_file, db, _resolve_seed_path())

        total = db.scalar(select(func.count()).select_from(Player)) or 0
        aligned = (
            db.scalar(
                select(func.count()).select_from(Player).where(Player.fpl_element_id.is_not(None))
            )
            or 0
        )
        db.add(
            IngestionRun(
                source=_INIT_SOURCE,
                job_name=_MIGRATE_ELEMENT_JOB_NAME,
                season_code=str(report.get("season_code") or SEASON_CODE),
                status="SUCCESS",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                records_processed=int(aligned),
            )
        )
        db.commit()
        return {
            "ok": True,
            "column_added": column_added,
            "backfilled_from_external_ids": backfilled,
            "players": int(total),
            "players_with_element_id": int(aligned),
            "seed_report": report,
        }
    except Exception as exc:  # noqa: BLE001 - surface for visibility
        db.rollback()
        logger.exception("migrate-fpl-element-id failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Phase 14.0 hotfix 2 — backfill real FPL photo codes from the updated seed
# --------------------------------------------------------------------------- #
# The original seed shipped code == id placeholders, so the deployed rows carry
# useless codes. Real element photo codes come from the live bootstrap API,
# which Vercel cannot reach (FPL blocks datacenter IPs) — so the committed seed
# was refreshed offline and this one-shot simply replays it: the player upsert
# backfills fpl_code on match. Seals itself after the first success (410).
# --------------------------------------------------------------------------- #

_RESEED_JOB_NAME = "reseed-fpl-codes"


def _reseed_applied(db: Session) -> bool:
    return (
        db.scalar(
            select(IngestionRun).where(
                IngestionRun.job_name == _RESEED_JOB_NAME,
                IngestionRun.source == _INIT_SOURCE,
                IngestionRun.status == "SUCCESS",
            )
        )
        is not None
    )


@router.post("/admin/reseed-fpl-codes")
async def reseed_fpl_codes_endpoint() -> dict:
    """Replay the bootstrap seed so every player gets their real FPL code.

    Returns 410 after the first successful run. Unauthenticated by design:
    temporary, self-disabling one-shot for the fresh deployment.
    """
    db = SessionLocal()
    try:
        if _reseed_applied(db):
            return JSONResponse(
                status_code=410,
                content={
                    "ok": False,
                    "error": "Reseed already applied. This endpoint is disabled "
                    "after its first successful run.",
                },
            )
        report = await run_in_threadpool(_seed_from_file, db, _resolve_seed_path())
        total = db.scalar(select(func.count()).select_from(Player)) or 0
        coded = (
            db.scalar(select(func.count()).select_from(Player).where(Player.fpl_code.is_not(None)))
            or 0
        )
        db.add(
            IngestionRun(
                source=_INIT_SOURCE,
                job_name=_RESEED_JOB_NAME,
                season_code=str(report.get("season_code") or SEASON_CODE),
                status="SUCCESS",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                records_processed=int(coded),
            )
        )
        db.commit()
        return {
            "ok": True,
            "players": int(total),
            "players_with_code": int(coded),
            "seed_report": report,
        }
    except Exception as exc:  # noqa: BLE001 - surface for visibility
        db.rollback()
        logger.exception("reseed-fpl-codes failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Phase 19.0 hotfix — create the sync-layer tables on the deployed database.
# Vercel's build never runs migrations, so the five new Phase 19 tables must be
# applied out-of-band exactly like the 0016 element-id hotfix above. All DDL is
# idempotent (IF NOT EXISTS). Seals itself after the first success (410).
# --------------------------------------------------------------------------- #

_MIGRATE_SYNC_JOB_NAME = "migrate-sync-tables"

_SYNC_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sync_live_points (
        id SERIAL PRIMARY KEY,
        gameweek INTEGER NOT NULL,
        element_id INTEGER NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        minutes INTEGER,
        fixture_text VARCHAR(120),
        opponent VARCHAR(60),
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT uq_sync_live_gw_element UNIQUE (gameweek, element_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_sync_live_points_gameweek ON sync_live_points (gameweek)",
    "CREATE INDEX IF NOT EXISTS ix_sync_live_points_element_id ON sync_live_points (element_id)",
    """
    CREATE TABLE IF NOT EXISTS ingested_history (
        id SERIAL PRIMARY KEY,
        gameweek INTEGER NOT NULL,
        element_id INTEGER NOT NULL,
        source VARCHAR(60) NOT NULL DEFAULT 'github-actions',
        total_points INTEGER NOT NULL DEFAULT 0,
        minutes INTEGER,
        bonus INTEGER,
        goals_scored INTEGER,
        assists INTEGER,
        xgi DOUBLE PRECISION,
        payload JSON NOT NULL DEFAULT '{}',
        ingested_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT uq_ingested_gw_element UNIQUE (gameweek, element_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_ingested_history_gameweek ON ingested_history (gameweek)",
    "CREATE INDEX IF NOT EXISTS ix_ingested_history_element_id ON ingested_history (element_id)",
    """
    CREATE TABLE IF NOT EXISTS recommendation (
        id SERIAL PRIMARY KEY,
        session_key VARCHAR(255) NOT NULL,
        gameweek INTEGER NOT NULL,
        rec_type VARCHAR(30) NOT NULL,
        subject JSON NOT NULL,
        detail JSON NOT NULL DEFAULT '{}',
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        scored_at TIMESTAMP WITH TIME ZONE,
        score JSON
    )""",
    "CREATE INDEX IF NOT EXISTS ix_recommendation_session_key ON recommendation (session_key)",
    "CREATE INDEX IF NOT EXISTS ix_recommendation_gameweek ON recommendation (gameweek)",
    """
    CREATE TABLE IF NOT EXISTS prediction_ledger (
        id SERIAL PRIMARY KEY,
        gameweek INTEGER NOT NULL,
        element_id INTEGER NOT NULL,
        predicted DOUBLE PRECISION NOT NULL,
        actual INTEGER,
        source VARCHAR(60) NOT NULL DEFAULT 'baseline-model',
        reconciled_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT uq_pred_ledger_gw_element UNIQUE (gameweek, element_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_prediction_ledger_gameweek ON prediction_ledger (gameweek)",
    "CREATE INDEX IF NOT EXISTS ix_prediction_ledger_element_id ON prediction_ledger (element_id)",
    """
    CREATE TABLE IF NOT EXISTS sync_log (
        id SERIAL PRIMARY KEY,
        kind VARCHAR(40) NOT NULL,
        entry_id VARCHAR(255),
        gameweek INTEGER,
        ok BOOLEAN NOT NULL DEFAULT TRUE,
        detail JSON NOT NULL DEFAULT '{}',
        created_at TIMESTAMP WITH TIME ZONE NOT NULL
    )""",
)


def _sync_migration_applied(db: Session) -> bool:
    return (
        db.scalar(
            select(IngestionRun).where(
                IngestionRun.job_name == _MIGRATE_SYNC_JOB_NAME,
                IngestionRun.source == _INIT_SOURCE,
                IngestionRun.status == "SUCCESS",
            )
        )
        is not None
    )


@router.post("/admin/migrate-sync-tables")
async def migrate_sync_tables_endpoint() -> dict:
    """Create the five Phase 19 sync tables; idempotent DDL.

    Returns 410 after the first successful run. Unauthenticated by design:
    temporary, self-disabling one-shot migration for the deployment.
    """
    db = SessionLocal()
    try:
        if _sync_migration_applied(db):
            return JSONResponse(
                status_code=410,
                content={
                    "ok": False,
                    "error": "Sync-table migration already applied. This endpoint "
                    "is disabled after its first successful run.",
                },
            )

        insp = sa_inspect(db.get_bind())
        existing_before = {
            name
            for name in (
                "sync_live_points",
                "ingested_history",
                "recommendation",
                "prediction_ledger",
                "sync_log",
            )
            if insp.has_table(name)
        }
        created_now: list[str] = []
        for ddl in _SYNC_DDL:
            db.execute(text(ddl))
        db.commit()

        # Re-inspect to report what actually appeared.
        insp2 = sa_inspect(db.get_bind())
        for name in (
            "sync_live_points",
            "ingested_history",
            "recommendation",
            "prediction_ledger",
            "sync_log",
        ):
            if name not in existing_before and insp2.has_table(name):
                created_now.append(name)

        if insp2.has_table("alembic_version"):
            row = db.execute(text("SELECT version_num FROM alembic_version")).first()
            current = row[0] if row else None
            if current and current < "0017":
                db.execute(text("DELETE FROM alembic_version"))
                db.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0017')"))
                db.commit()

        db.add(
            IngestionRun(
                source=_INIT_SOURCE,
                job_name=_MIGRATE_SYNC_JOB_NAME,
                season_code=SEASON_CODE,
                status="SUCCESS",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                records_processed=len(created_now),
            )
        )
        db.commit()
        return {"ok": True, "tables_created": created_now}
    except Exception as exc:  # noqa: BLE001 - surface for visibility
        db.rollback()
        logger.exception("migrate-sync-tables failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Phase 20.0 � Friday 18:00 UTC assistant-brief push (Vercel Cron)
# --------------------------------------------------------------------------- #

_FRIDAY_BRIEF_MAX_SQUADS = 20


def _format_brief_message(brief: dict, entry_name: str | None) -> str:
    """Plain-text Telegram rendering of the six brief sections."""
    sections = brief.get("sections") or {}
    titles = [
        "SQUAD STATUS", "CAPTAIN", "TRANSFERS",
        "FIXTURE SWINGS", "NEWS FLAGS", "LAST WEEK GRADE",
    ]
    lines = [f"?? Weekly brief � GW{brief.get('gameweek', '?')}"
             + (f" � {entry_name}" if entry_name else ""), ""]
    for t in titles:
        body = sections.get(t)
        if isinstance(body, str) and body.strip():
            lines.append(f"<b>{t}</b>")
            lines.append(body.strip())
            lines.append("")
    model = brief.get("model") or "template-fallback"
    lines.append(f"� {model}")
    return "\n".join(lines)


@router.get("/admin/friday-brief")
@router.post("/admin/friday-brief")
async def friday_brief_endpoint(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """Push the weekly assistant brief to Telegram every Friday 18:00 UTC.

    Builds a brief per saved squad (capped), then sends it to every allowed
    chat when TELEGRAM_BOT_TOKEN is configured. Without the token the endpoint
    still returns 200 with ``pushed: false`` so cron runs stay green.
    """
    _require_cron_auth(authorization)

    from fpl_intelligence.api.routes.assistant import assistant_brief
    from fpl_intelligence.db.models import SquadStateDB
    from fpl_intelligence.notifications.telegram_bot import get_allowed_user_ids

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = get_allowed_user_ids()

    db = SessionLocal()
    try:
        rows = db.execute(
            select(SquadStateDB.session_id).order_by(SquadStateDB.updated_at.desc())
        ).scalars().all()[:_FRIDAY_BRIEF_MAX_SQUADS]

        built = 0
        pushed = 0
        errors: list[str] = []
        client: httpx.AsyncClient | None = None
        try:
            if token and chat_ids and rows:
                client = httpx.AsyncClient(timeout=10.0)
            for session_id in rows:
                try:
                    brief = await assistant_brief(
                        response=Response(), db=db, session_id=session_id, gw=None
                    )
                    built += 1
                except Exception as exc:  # noqa: BLE001 — one bad squad never stops the run
                    errors.append(f"{session_id}: {type(exc).__name__}")
                    continue
                if client is None:
                    continue
                entry_name = ((brief.get("facts_digest") or {}).get("captain")) or "squad"
                text = _format_brief_message(brief, str(entry_name))
                for chat_id in chat_ids:
                    try:
                        r = await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                        )
                        r.raise_for_status()
                        pushed += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Friday brief push failed chat %s: %s", chat_id, exc)
        finally:
            if client is not None:
                await client.aclose()

        return {
            "ok": True,
            "squads": len(rows),
            "briefs_built": built,
            "telegram_configured": bool(token and chat_ids),
            "pushed": pushed,
            "errors": errors[:5],
        }
    finally:
        db.close()

# --------------------------------------------------------------------------- #
# Phase 20.1 — materialization cron (06:10 UTC) + self-sealing DDL hotfix
# --------------------------------------------------------------------------- #
# The prod incident root cause: request paths computed everything inline and
# hit FPL endpoints that are blocked from Vercel datacenter IPs (drawer 504,
# stale/wrong fixtures, slow navigation). The fix is "materialize, don't
# compute per request": one daily cron fetches vaastav GW results, fixtures,
# BBC RSS and precomputes per-player xPTS for the next 5 GWs into read-model
# tables. Request paths then serve from indexed tables with zero egress.
# --------------------------------------------------------------------------- #

_MIGRATE_MATERIALIZED_JOB_NAME = "migrate-materialized-tables"

_MATERIALIZED_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS fixtures_cache (
        id SERIAL PRIMARY KEY,
        source VARCHAR(120) NOT NULL DEFAULT 'vaastav',
        payload JSONB NOT NULL,
        fetched_at TIMESTAMP WITH TIME ZONE NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_cache (
        id SERIAL PRIMARY KEY,
        source VARCHAR(120) NOT NULL DEFAULT 'bbc-rss',
        headline_count INTEGER NOT NULL DEFAULT 0,
        payload JSONB NOT NULL,
        fetched_at TIMESTAMP WITH TIME ZONE NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS element_facts (
        element_id INTEGER PRIMARY KEY,
        web_name VARCHAR(120),
        team_id INTEGER,
        minutes INTEGER,
        selected_by_percent VARCHAR(20),
        cost_change_event INTEGER,
        status VARCHAR(20),
        news VARCHAR(500),
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS predictions_current (
        gameweek INTEGER NOT NULL,
        element_id INTEGER NOT NULL,
        expected_points DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        minutes_estimate DOUBLE PRECISION,
        start_prob DOUBLE PRECISION,
        xg_per_90 DOUBLE PRECISION,
        xa_per_90 DOUBLE PRECISION,
        source VARCHAR(60),
        data_quality VARCHAR(60),
        breakdown JSONB,
        computed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT uq_pred_current_gw_element UNIQUE (gameweek, element_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_predictions_current_element "
    "ON predictions_current (element_id)",
)


def _materialized_migration_applied(db: Session) -> bool:
    return (
        db.scalar(
            select(IngestionRun).where(
                IngestionRun.job_name == _MIGRATE_MATERIALIZED_JOB_NAME,
                IngestionRun.source == _INIT_SOURCE,
                IngestionRun.status == "SUCCESS",
            )
        )
        is not None
    )


@router.post("/admin/migrate-materialized-tables")
async def migrate_materialized_tables_endpoint() -> dict:
    """Create the four Phase 20.1 read-model tables; idempotent DDL.

    Returns 410 after the first successful run. Unauthenticated by design:
    temporary, self-disabling one-shot migration for the deployment.
    """
    db = SessionLocal()
    try:
        if _materialized_migration_applied(db):
            return JSONResponse(
                status_code=410,
                content={
                    "ok": False,
                    "error": "Materialized-table migration already applied. This "
                    "endpoint is disabled after its first successful run.",
                },
            )

        table_names = (
            "fixtures_cache",
            "news_cache",
            "element_facts",
            "predictions_current",
        )
        insp = sa_inspect(db.get_bind())
        existing_before = {n for n in table_names if insp.has_table(n)}
        created_now: list[str] = []
        for ddl in _MATERIALIZED_DDL:
            db.execute(text(ddl))
        db.commit()

        insp2 = sa_inspect(db.get_bind())
        for name in table_names:
            if name not in existing_before and insp2.has_table(name):
                created_now.append(name)

        if insp2.has_table("alembic_version"):
            row = db.execute(text("SELECT version_num FROM alembic_version")).first()
            current = row[0] if row else None
            if current and current < "0018":
                db.execute(text("DELETE FROM alembic_version"))
                db.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES ('0018')")
                )
                db.commit()

        db.add(
            IngestionRun(
                source=_INIT_SOURCE,
                job_name=_MIGRATE_MATERIALIZED_JOB_NAME,
                season_code=SEASON_CODE,
                status="SUCCESS",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                records_processed=len(created_now),
            )
        )
        db.commit()
        return {"ok": True, "tables_created": created_now}
    except Exception as exc:  # noqa: BLE001 - surface for visibility
        db.rollback()
        logger.exception("migrate-materialized-tables failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        db.close()


@router.get("/admin/materialize")
@router.post("/admin/materialize")
async def materialize_endpoint(
    _: None = Depends(_require_cron_auth),
) -> dict:
    """Phase 20.1 — run the full materialization pipeline.

    Fetches (from Vercel-reachable sources only): vaastav GW results +
    fixtures.csv + players_raw.csv via raw.githubusercontent, BBC Sport RSS
    directly — then precomputes per-player xPTS for the next 5 gameweeks into
    ``predictions_current``. Idempotent; safe to re-run any time.
    """
    from fpl_intelligence.materialize import materialize_all

    db = SessionLocal()
    try:
        report = await materialize_all(db, season_code=SEASON_CODE)
        ok = bool(report.get("predictions", {}).get("ok")) or bool(
            report.get("fixtures", {}).get("ok")
        )
        return JSONResponse(status_code=200 if ok else 502, content={"ok": ok, **report})
    except Exception as exc:  # noqa: BLE001 - surface for cron visibility
        db.rollback()
        logger.exception("materialize failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        db.close()


_BOOTSTRAP_MATERIALIZED_JOB_NAME = "materialize-bootstrap"


def _bootstrap_materialized_applied(db: Session) -> bool:
    return (
        db.scalar(
            select(IngestionRun).where(
                IngestionRun.job_name == _BOOTSTRAP_MATERIALIZED_JOB_NAME,
                IngestionRun.source == _INIT_SOURCE,
                IngestionRun.status == "SUCCESS",
            )
        )
        is not None
    )


@router.post("/admin/bootstrap-materialized")
async def bootstrap_materialized_endpoint() -> dict:
    """Phase 20.1 — one-shot incident recovery: populate the read models NOW.

    Runs :func:`materialize_all` once so the deployed engine serves warm data
    immediately instead of waiting for the 06:10 cron. Self-disabling: returns
    410 after its first successful run (same pattern as the migration hotfixes).
    Unauthenticated by design: temporary, one-shot, and only writes derived
    cache tables from public sources.
    """
    db = SessionLocal()
    try:
        if _bootstrap_materialized_applied(db):
            return JSONResponse(
                status_code=410,
                content={
                    "ok": False,
                    "error": "Materialized bootstrap already applied. This endpoint "
                    "is disabled; use POST /admin/materialize (cron auth).",
                },
            )

        from fpl_intelligence.materialize import materialize_all

        report = await materialize_all(db, season_code=SEASON_CODE)
        db.add(
            IngestionRun(
                source=_INIT_SOURCE,
                job_name=_BOOTSTRAP_MATERIALIZED_JOB_NAME,
                season_code=SEASON_CODE,
                status="SUCCESS",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                records_processed=int(report.get("predictions", {}).get("rows") or 0),
            )
        )
        db.commit()
        ok = bool(report.get("predictions", {}).get("ok")) or bool(
            report.get("fixtures", {}).get("ok")
        )
        return JSONResponse(status_code=200 if ok else 502, content={"ok": ok, **report})
    except Exception as exc:  # noqa: BLE001 - surface for visibility
        db.rollback()
        logger.exception("bootstrap-materialized failed")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Phase 20.4 — THE daily job (single consolidated cron, 06:10 UTC).
#
# vercel.json carries EXACTLY ONE cron entry pointing here. It runs, in order:
#   1. materialize        — vaastav GW results + fixtures + BBC RSS + xPTS
#   2. pending squad syncs— retry any queued auto-sync
#   3. pre-generate brief — current-GW assistant brief for every saved squad
#   4. grade              — score recommendations for finished gameweeks
# The run itself is recorded as an IngestionRun(job_name="daily") so the
# Sources page can show "daily job last run {time}" honestly.
# --------------------------------------------------------------------------- #

DAILY_JOB_NAME = "daily"
_DAILY_MAX_SQUADS = _FRIDAY_BRIEF_MAX_SQUADS

_LIVE_SNAPSHOTS_DDL = (
    """
    CREATE TABLE IF NOT EXISTS live_snapshots (
        id SERIAL PRIMARY KEY,
        gameweek INTEGER NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        fetched_at TIMESTAMP WITH TIME ZONE NOT NULL
    )
    """,
    # Phase 13.5 legacy table some prod DBs never received.
    """
    CREATE TABLE IF NOT EXISTS pending_sync (
        id SERIAL PRIMARY KEY,
        entry_id INTEGER NOT NULL,
        auto_sync BOOLEAN NOT NULL DEFAULT TRUE,
        status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
    )
    """,
)

# Tables ensured lazily by _ensure_daily_tables (idempotent DDL).
_ENSURE_TABLES = ("live_snapshots", "pending_sync")


def _ensure_daily_tables(db: Session) -> list[str]:
    created: list[str] = []
    insp = sa_inspect(db.get_bind())
    existing = {name for name in _ENSURE_TABLES if insp.has_table(name)}
    if len(existing) == len(_ENSURE_TABLES):
        return []
    for ddl in _LIVE_SNAPSHOTS_DDL:
        try:
            db.execute(text(ddl))
            db.commit()
        except Exception as exc:  # noqa: BLE001 - sqlite tests pre-create them
            db.rollback()
            logger.debug("daily DDL skipped: %s", exc)
    insp2 = sa_inspect(db.get_bind())
    created = [n for n in _ENSURE_TABLES if n not in existing and insp2.has_table(n)]
    return created


def _finished_gameweek_from_cache(payload: list) -> int | None:
    """Highest gameweek whose fixtures are ALL marked finished (pure)."""
    by_gw: dict[int, dict[str, int]] = {}
    for item in payload or []:
        try:
            gwi = int(item.get("event"))
        except (TypeError, ValueError):
            continue
        bucket = by_gw.setdefault(gwi, {"total": 0, "finished": 0})
        bucket["total"] += 1
        if item.get("finished"):
            bucket["finished"] += 1
    complete = [gw for gw, b in by_gw.items() if b["finished"] == b["total"]]
    return max(complete) if complete else None


@router.get("/admin/daily")
@router.post("/admin/daily")
async def daily_endpoint(
    _: None = Depends(_require_cron_auth),
) -> dict:
    """Phase 20.4 — run the whole day's work in one authenticated request.

    A global watchdog bounds the entire pipeline to ~48 s so the endpoint
    ALWAYS answers inside the 60 s serverless budget. Stages that do not
    finish are reported honestly as deferred and simply complete on the next
    run (every stage is idempotent).
    """
    import asyncio
    import contextlib
    import time as _time

    from fpl_intelligence.api.routes.assistant import assistant_brief
    from fpl_intelligence.materialize import materialize_all
    from fpl_intelligence.notifications.telegram_bot import get_allowed_user_ids
    from fpl_intelligence.squad.models_db import SquadStateDB
    from fpl_intelligence.squad.sync import NoPendingSync, run_pending_sync
    from fpl_intelligence.sync.materialized_models import FixturesCacheDB
    from fpl_intelligence.sync.service import score_pending_recommendations

    started_at = datetime.now(UTC)
    steps: dict[str, dict] = {}
    db = SessionLocal()
    total_steps = 4

    async def _run_stages() -> int:
        """All four stages. Returns newly_scored count."""
        # -- 0. self-sealing DDL -------------------------------------------------
        created_tables = _ensure_daily_tables(db)
        steps["tables"] = {"ok": True, "detail": created_tables or "up to date"}

        # -- 1. materialize (cap 25 s) --------------------------------------------
        t0 = _time.monotonic()
        try:
            report = await asyncio.wait_for(
                materialize_all(db, season_code=SEASON_CODE), timeout=25.0
            )
            mat_ok = bool(report.get("predictions", {}).get("ok")) or bool(
                report.get("fixtures", {}).get("ok")
            )
            steps["materialize"] = {
                "ok": mat_ok,
                "ms": int((_time.monotonic() - t0) * 1000),
                "detail": {
                    k: (v or {}).get("ok") for k, v in report.items() if isinstance(v, dict)
                },
            }
        except TimeoutError:
            db.rollback()
            steps["materialize"] = {"ok": False, "detail": "deferred — 25s stage budget"}
        except Exception as exc:  # noqa: BLE001 - one step never stops the rest
            db.rollback()
            steps["materialize"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

        # -- 2. pending squad syncs (cap 12 s) --------------------------------------
        t0 = _time.monotonic()
        try:
            try:
                result = await asyncio.wait_for(run_pending_sync(db), timeout=12.0)
                steps["sync"] = {
                    "ok": True,
                    "ms": int((_time.monotonic() - t0) * 1000),
                    "detail": f"entry {result.entry_id} synced",
                }
            except NoPendingSync:
                steps["sync"] = {"ok": True, "detail": "no pending sync"}
        except TimeoutError:
            steps["sync"] = {"ok": False, "detail": "deferred — 12s stage budget"}
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            steps["sync"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

        # -- 3. grade any finished ungraded gameweek ----------------------------------
        graded_note = "nothing to grade"
        newly_scored_local = 0
        try:
            fx_row = db.scalar(
                select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()).limit(1)
            )
            fin_gw = _finished_gameweek_from_cache((fx_row.payload or []) if fx_row else [])
            if fin_gw is not None:
                newly_scored_local = score_pending_recommendations(db, up_to_gameweek=fin_gw)
                db.commit()
                graded_note = f"GW{fin_gw}: {newly_scored_local} recommendation(s) scored"
            else:
                graded_note = "no fully-finished gameweek in fixtures cache yet"
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            graded_note = f"{type(exc).__name__}: {exc}"
        steps["grading"] = {"ok": True, "detail": graded_note}

        # -- 4. pre-generate current-GW briefs (runs LAST, hard 12 s stage cap) --------
        # Cheap stages always complete first; deferred squads lazily generate on
        # first page load instead (identical code path).
        built = 0
        skipped = 0
        timed_out = 0
        brief_errors: list[str] = []

        async def _build_briefs():
            nonlocal built, skipped, timed_out
            rows = db.execute(
                select(SquadStateDB.session_id).order_by(SquadStateDB.updated_at.desc())
            ).scalars().all()[:_DAILY_MAX_SQUADS]
            for sid in rows:
                if _time.monotonic() - started_at.timestamp() > 34.0:
                    skipped += 1
                    continue
                try:
                    await asyncio.wait_for(
                        assistant_brief(response=Response(), db=db, session_id=str(sid), gw=None),
                        timeout=10.0,
                    )
                    built += 1
                except TimeoutError:
                    timed_out += 1
                except Exception as exc:  # noqa: BLE001 - per-squad isolation
                    brief_errors.append(f"{sid}: {type(exc).__name__}")

        t0 = _time.monotonic()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_build_briefs(), timeout=12.0)
        session_count = len(
            db.execute(select(SquadStateDB.session_id)).scalars().all()[:_DAILY_MAX_SQUADS]
        )
        steps["briefs"] = {
            "ok": True,
            "ms": int((_time.monotonic() - t0) * 1000),
            "detail": {
                "built": built,
                "squads": min(session_count, _DAILY_MAX_SQUADS),
                "deferred": skipped + timed_out,
                "errors": brief_errors[:5],
            },
        }
        return newly_scored_local

    ok_count = 0
    newly_scored = 0
    finished_at = started_at
    watchdog_hit = False
    try:
        try:
            newly_scored = await asyncio.wait_for(_run_stages(), timeout=48.0)
        except TimeoutError:
            watchdog_hit = True
            for name in ("tables", "materialize", "sync", "briefs", "grading"):
                steps.setdefault(name, {"ok": False, "detail": "deferred — global watchdog"})
            logger.warning("daily job hit the 48s global watchdog")
        finally:
            finished_at = datetime.now(UTC)

        names = ("materialize", "sync", "briefs", "grading")
        ok_count = sum(1 for name in names if steps.get(name, {}).get("ok"))
        status = "SUCCESS" if ok_count == total_steps else ("PARTIAL" if ok_count else "FAILED")
        db.add(
            IngestionRun(
                source=_INIT_SOURCE,
                job_name=DAILY_JOB_NAME,
                season_code=SEASON_CODE,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                records_processed=ok_count,
            )
        )
        db.commit()

        # -- optional final-whistle Telegram summary after grading --------------------
        telegram_summary: dict = {}
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_ids = get_allowed_user_ids()
        if newly_scored and token and chat_ids:
            text = (
                f"Final whistle: {newly_scored} recommendation(s) just graded. "
                "Open Track Record for the verdicts."
            )
            pushed = 0
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    for chat_id in chat_ids:
                        r = await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": text},
                        )
                        r.raise_for_status()
                        pushed += 1
            except Exception as exc:  # noqa: BLE001 - summary is best-effort
                logger.warning("final-whistle push failed: %s", exc)
            telegram_summary = {"attempted": True, "pushed": pushed}
        else:
            telegram_summary = {"attempted": False, "pushed": 0}

        return JSONResponse(
            status_code=200 if ok_count == total_steps else 207,
            content={
                "ok": ok_count == total_steps,
                "job": DAILY_JOB_NAME,
                "steps_ok": f"{ok_count}/{total_steps}",
                "watchdog": watchdog_hit,
                "steps": steps,
                "graded_now": newly_scored,
                "telegram_summary": telegram_summary,
                "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
            },
        )
    except Exception as exc:  # noqa: BLE001 - surface for cron visibility
        db.rollback()
        logger.exception("daily job failed")
        finished_at = datetime.now(UTC)
        try:
            db.add(
                IngestionRun(
                    source=_INIT_SOURCE,
                    job_name=DAILY_JOB_NAME,
                    season_code=SEASON_CODE,
                    status="FAILED",
                    started_at=started_at,
                    finished_at=finished_at,
                    records_processed=0,
                )
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        db.close()
