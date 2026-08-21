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
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
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
        "fpl_bootstrap_seed.json not found; looked in "
        + ", ".join(str(c) for c in candidates)
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

    POST /api/v1/admin/seed-from-file?secret=<CRON_SECRET> (or with the
    ``Authorization: Bearer <CRON_SECRET>`` header). Safe to call repeatedly.
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
            db.scalar(
                select(func.count())
                .select_from(Player)
                .where(Player.fpl_code.is_not(None))
            )
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
            db.scalar(
                select(func.count())
                .select_from(Player)
                .where(Player.fpl_code.is_not(None))
            )
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

