"""Phase 17.0 — Data Sources status endpoint.

Surfaces the live status of every external data source the engine depends on:
FPL import, Odds API, Understat, Weather, PL photos, and the LLM. This is the
answer to "where is the AI / where is the math / why is X off".
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Response
from sqlalchemy import func

from fpl_intelligence.api import deps
from fpl_intelligence.config import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)


def _file_age_days(path: str) -> float | None:
    try:
        age = time.time() - os.path.getmtime(path)
        return round(age / 86400.0, 1)
    except OSError:
        return None


def _snapshot_age_and_seasons(path: str) -> tuple[float | None, list[str]]:
    """Phase 21.1 (T5): snapshot age from its OWN meta.fetched_at timestamp.

    The previous mtime-based age reported absurd values on Vercel (bundled
    files carry arbitrary mtimes, e.g. "2864.8d old"). Reading the fetched_at
    the connector itself wrote is deterministic and honest; mtime is only a
    fallback when meta is unreadable.
    """
    try:
        import json

        with open(path, encoding="utf-8") as fh:
            meta = (json.load(fh) or {}).get("meta") or {}
        fetched_raw = str(meta.get("fetched_at") or "")
        if fetched_raw:
            fetched = datetime.fromisoformat(fetched_raw.replace("Z", "+00:00"))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=UTC)
            age_days = max(0.0, (datetime.now(UTC) - fetched).total_seconds()) / 86400.0
            seasons = [
                str(s)
                for s in (
                    meta.get("seasons") or []
                    if isinstance(meta.get("seasons"), list)
                    else []
                )
            ]
            return round(age_days, 1), seasons
    except Exception:  # noqa: BLE001 - fall back to file metadata below
        pass
    return _file_age_days(path), []


def _in_pytest() -> bool:
    """True under pytest so live probes never run inside the test suite."""
    import os
    import sys

    return "pytest" in sys.modules or os.environ.get("FPL_NO_NETWORK", "") == "1"


_ODDS_PROBE_TTL_SECONDS = 1800.0
_odds_probe_cache: tuple[float, dict[str, Any] | None] = (0.0, None)


async def _probe_odds(db: Any) -> dict[str, Any]:
    """Phase 21.1 (T4): odds coverage over the next unplayed gameweek.

    Returns ``{status, detail}`` where detail reads ``matched N/M`` plus the
    unmatched club names (also logged) so mapping gaps are visible instead of
    silently zeroing the market check.
    """
    if _in_pytest():
        return {"status": "off", "detail": "probe disabled in tests"}
    now_mono = time.monotonic()
    with _response_lock:
        cached = _odds_probe_cache
    if cached[1] is not None and now_mono - cached[0] < _ODDS_PROBE_TTL_SECONDS:
        return cached[1]

    result = await _probe_odds_uncached(db)
    with _response_lock:
        globals()["_odds_probe_cache"] = (time.monotonic(), result)
    return result


async def _probe_odds_uncached(db: Any) -> dict[str, Any]:
    """Phase 23 (C1): thin wrapper around the SHARED market-check computation.

    All matching/detail formatting lives in
    :mod:`fpl_intelligence.prediction.market_check` so this page renders the
    exact same sentence as the Decisions banner and Captain Spotlight.
    """
    api_key = os.getenv("THE_ODDS_API_KEY", "").strip()
    if not api_key:
        return {"status": "off", "detail": "THE_ODDS_API_KEY not set"}

    def _fetch() -> Any:
        from fpl_intelligence.data_providers.odds_api import OddsApiConnector

        connector = OddsApiConnector(api_key=api_key, timeout=4.0)
        try:
            return connector.fetch_epl_odds()
        finally:
            connector.close()

    try:
        snapshot = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=8.0)
    except Exception as exc:  # noqa: BLE001 — graceful degradation contract
        return {"status": "blocked", "detail": f"odds fetch failed ({type(exc).__name__})"}
    if snapshot is None or not getattr(snapshot, "matches", []):
        return {"status": "degraded", "detail": "no h2h markets returned"}

    try:
        block = await odds_probe_payload(db, snapshot)
        if block.get("unmatched"):
            logger.info("odds mapping unmatched teams: %s", block["unmatched"])
        # Phase 23 (C1): persist the canonical payload so Decisions/Captain
        # (materialized fast path) render the exact same sentence.
        try:
            from fpl_intelligence.prediction.market_check import store_shared_payload

            store_shared_payload(
                db,
                block,
                gameweek=block.get("gameweek"),
            )
        except Exception:  # noqa: BLE001 — best-effort persistence
            pass
        return {"status": block["status"], "detail": block["detail"]}
    except Exception as exc:  # noqa: BLE001 — audit must never fail the page
        db.rollback()  # keep the shared request session usable afterwards
        return {
            "status": "ok",
            "detail": (
                f"{len(snapshot.matches)} EPL events cached "
                f"(mapping audit skipped: {type(exc).__name__})"
            ),
        }


async def odds_probe_payload(db: Any, snapshot: Any) -> dict[str, Any]:
    """Assembly of the Sources odds row through the shared module.

    Exposed for tests so the Sources surface can be asserted byte-identical to
    the Decisions/Captain payloads without network access.
    """
    from sqlalchemy import select

    from fpl_intelligence.db.models import Team, TeamExternalId
    from fpl_intelligence.fixtures.scanner import parse_fixtures
    from fpl_intelligence.materialize.service import load_cached_fixtures
    from fpl_intelligence.prediction.market_check import compute_market_status
    from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek

    covered = snapshot.matched_event_names()
    fixtures_raw = load_cached_fixtures(db)
    if not fixtures_raw:
        return {
            "status": "ok",
            "detail": f"{len(snapshot.matches)} EPL events cached — no fixtures yet",
            "unmatched": [],
            "enabled": False,
        }

    target_gw = await resolve_target_gameweek(db)
    parsed = parse_fixtures(fixtures_raw)
    gw_rows = [r for r in parsed if r.event == target_gw and not r.finished]
    if not gw_rows:
        upcoming = sorted({r.event for r in parsed if not r.finished})
        if upcoming:
            gw_rows = [r for r in parsed if r.event == upcoming[0]]

    id_to_names: dict[int, list[str]] = {}
    for provider_id, short_name, full_name in db.execute(
        select(TeamExternalId.provider_team_id, Team.short_name, Team.name).join(
            Team, Team.id == TeamExternalId.team_id
        ).where(TeamExternalId.provider == "official_fpl")
    ).all():
        if provider_id is None:
            continue
        id_to_names[int(provider_id)] = [
            str(c) for c in (short_name, full_name) if c
        ]

    rows = [(r.event, r.home_team, r.away_team) for r in gw_rows]
    status_block = compute_market_status(rows, id_to_names, covered)
    return {
        "status": status_block["status"],
        "detail": status_block["detail"],
        "unmatched": status_block["unmatched"],
        "fixtures_matched": status_block["fixtures_matched"],
        "fixtures_total": status_block["fixtures_total"],
        "gameweek": status_block["gameweek"],
        "enabled": status_block["fixtures_matched"] > 0,
    }


_UNDERSTAT_REFRESH_TTL_SECONDS = 900.0
_understat_refresh_cache: tuple[float, dict[str, Any] | None] = (0.0, None)


async def _probe_understat_refresh(db: Any) -> dict[str, Any]:
    """Phase 21.1 (T5): attempt a 2026/27 Understat refresh through the masks.

    On success the parsed player rows are persisted into ``provider_refresh``
    (Vercel FS is read-only, so the committed seed cannot be rewritten) and
    enrichment readers merge them over the offline snapshot. When blocked the
    label honestly says which season the snapshot covers.
    """
    if _in_pytest():
        from fpl_intelligence.data_providers.understat import UNDERSTAT_SNAPSHOT_PATH

        age, seasons = _snapshot_age_and_seasons(str(UNDERSTAT_SNAPSHOT_PATH))
        return {
            "status": "degraded",
            "detail": f"refresh probe disabled in tests (snapshot {age}d)",
        }
    now_mono = time.monotonic()
    with _response_lock:
        cached = _understat_refresh_cache
    if cached[1] is not None and now_mono - cached[0] < _UNDERSTAT_REFRESH_TTL_SECONDS:
        return cached[1]

    result = await _probe_understat_refresh_uncached(db)
    with _response_lock:
        globals()["_understat_refresh_cache"] = (time.monotonic(), result)
    return result


async def _probe_understat_refresh_uncached(db: Any) -> dict[str, Any]:
    from fpl_intelligence.data_providers.understat import (
        UNDERSTAT_SNAPSHOT_PATH,
        parse_hex_json_blocks,
    )

    snapshot_age, snapshot_seasons = _snapshot_age_and_seasons(str(UNDERSTAT_SNAPSHOT_PATH))
    covers_2026 = any(s.startswith("2026") for s in snapshot_seasons)

    def _season_label() -> str:
        base = "2026/27" if covers_2026 else "2025/26 season snapshot"
        age_txt = f"{snapshot_age:.1f}d old" if snapshot_age is not None else "age unknown"
        return f"{base} ({age_txt})"

    settings = get_settings()

    async def _attempt() -> tuple[list[dict[str, Any]] | None, str]:
        try:
            from fpl_intelligence.data_providers.fpl_egress import FplEgressChain

            egress = FplEgressChain(
                "https://understat.com",
                timeout=min(4.0, settings.egress_strategy_timeout),
                cache_ttl=0,
            )
            html = await egress.fetch("/league/EPL/2026")
            strategy = egress.winning_strategy or "direct"
        except Exception as exc:  # noqa: BLE001 - blocked is an expected outcome
            return None, f"{type(exc).__name__}: {exc}"
        datasets = parse_hex_json_blocks(html if isinstance(html, str) else "")
        players = datasets.get("playersData")
        if not isinstance(players, list) or not players:
            return None, "page reachable but no playersData block"
        return [row for row in players if isinstance(row, dict)], strategy

    try:
        player_rows, note = await asyncio.wait_for(_attempt(), timeout=10.0)
    except TimeoutError:
        player_rows, note = None, "refresh attempt exceeded 10s budget"

    if player_rows:
        stored = False
        try:
            from datetime import datetime as _dt

            from sqlalchemy import select, text

            from fpl_intelligence.sync.materialized_models import ProviderRefreshDB

            db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS provider_refresh ("
                    " source VARCHAR(60) PRIMARY KEY,"
                    " season_label VARCHAR(40),"
                    " player_count INTEGER NOT NULL DEFAULT 0,"
                    " payload JSONB NOT NULL DEFAULT '[]'::jsonb,"
                    " fetched_at TIMESTAMP WITH TIME ZONE NOT NULL)"
                )
            )
            row = db.scalar(
                select(ProviderRefreshDB).where(ProviderRefreshDB.source == "understat")
            )
            now = _dt.now(UTC)
            if row is None:
                row = ProviderRefreshDB(
                    source="understat",
                    season_label="2026/27",
                    player_count=len(player_rows),
                    payload=player_rows[:800],
                    fetched_at=now,
                )
                db.add(row)
            else:
                row.season_label = "2026/27"
                row.player_count = len(player_rows)
                row.payload = player_rows[:800]
                row.fetched_at = now
            db.commit()
            stored = True
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            db.rollback()
            logger.warning("understat refresh persistence failed: %s", exc)
        detail = (
            f"2026/27 live via {note} · {len(player_rows)} players"
            + ("" if stored else " · not persisted")
        )
        return {"status": "ok", "detail": detail}

    label = _season_label()
    return {
        "status": "stale" if covers_2026 is False else "degraded",
        "detail": f"{label} — 2026/27 refresh blocked ({note})",
    }


def _age_seconds_since(when: datetime) -> float:
    """Age of a stored timestamp in seconds (naive values treated as UTC)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - when).total_seconds())


#: Phase 20.1 — the two live reachability probes (FPL, PL photos CDN) used to
#: run sequentially with 8s timeouts each, which made the Sources page take
#: ~6s+ on Vercel where FPL is blocked. They now run concurrently with tight
#: budgets and the assembled payload is cached briefly in-process.
_PROBE_BUDGET_SECONDS = 3.0
_RESPONSE_CACHE_SECONDS = 60.0
_response_cache: tuple[float, dict[str, Any] | None] = (0.0, None)
_response_lock = threading.Lock()


async def _probe_fpl(settings: Any) -> tuple[str, str, str]:
    """FPL import reachability -> (status, detail, strategy)."""
    try:
        from fpl_intelligence.data_providers.fpl_egress import (  # noqa: PLC0415
            FplEgressChain,
            validate_entry_payload,
        )

        egress = FplEgressChain(
            settings.fpl_base_url,
            timeout=min(_PROBE_BUDGET_SECONDS, settings.egress_strategy_timeout),
            cache_ttl=0,  # never cache a health probe
        )
        await egress.fetch("/api/entry/1/", validator=validate_entry_payload)
        return "ok", "reachable", egress.winning_strategy or "direct"
    except Exception:  # noqa: BLE001
        pass
    try:
        async with httpx.AsyncClient(
            timeout=_PROBE_BUDGET_SECONDS, follow_redirects=True
        ) as client:
            r = await client.get(
                f"{settings.fpl_base_url.rstrip('/')}/api/entry/1/",
                headers={
                    "User-Agent": "FPL-Intelligence-Engine/1.0",
                    "Accept": "application/json",
                },
            )
        if r.status_code == 200:
            return "ok", "reachable", "direct"
        if r.status_code == 403:
            return "blocked", "rate-limited by FPL", ""
        return "degraded", f"HTTP {r.status_code}", ""
    except Exception as inner:  # noqa: BLE001
        return "blocked", f"unreachable ({type(inner).__name__})", ""


async def _probe_photos() -> tuple[str, str]:
    """Premier League CDN reachability -> (status, detail)."""
    try:
        async with httpx.AsyncClient(
            timeout=_PROBE_BUDGET_SECONDS, follow_redirects=True
        ) as client:
            r = await client.head("https://resources.premierleague.com/badges/70/t1.png")
        if r.status_code < 400:
            return "ok", "Premier League CDN"
        return "outage", f"HTTP {r.status_code}"
    except Exception:  # noqa: BLE001
        return "outage", "unreachable — avatars fallback active"


@router.get("/data-sources", summary="Live status of every data source")
async def data_sources(db: deps.GetDB, response: Response) -> dict[str, Any]:
    """Return the live status of each external data source."""
    # Status payloads must never be cached by browsers — the Sources page
    # otherwise shows a stale snapshot from an earlier navigation (Phase 20.1).
    response.headers["Cache-Control"] = "no-store"
    now_mono = time.monotonic()
    with _response_lock:
        cached = _response_cache
    if cached[1] is not None and now_mono - cached[0] < _RESPONSE_CACHE_SECONDS:
        return cached[1]

    settings = get_settings()

    # Live probes run concurrently so the worst case is one probe budget.
    fpl_task = asyncio.create_task(_probe_fpl(settings))
    photos_task = asyncio.create_task(_probe_photos())
    odds_task = asyncio.create_task(_probe_odds(db))
    understat_task = asyncio.create_task(_probe_understat_refresh(db))
    fpl_status, fpl_detail, fpl_strategy = await fpl_task
    photos_status, photos_detail = await photos_task
    try:
        odds_block = await asyncio.wait_for(odds_task, timeout=10.0)
    except TimeoutError:
        odds_block = {"status": "degraded", "detail": "odds probe over budget"}
    try:
        understat_block = await asyncio.wait_for(understat_task, timeout=12.0)
    except TimeoutError:
        understat_block = {
            "status": "degraded",
            "detail": "understat refresh attempt over budget",
        }

    now = datetime.now(UTC).isoformat()

    # --- Phase 21.1 (T4): odds mapping audit ----------------------------------
    odds_status = str(odds_block.get("status", "off"))
    odds_detail = str(odds_block.get("detail", ""))

    # --- Understat: honest season label + refresh attempt ---------------------
    understat_status = str(understat_block.get("status", "stale"))
    understat_detail = str(understat_block.get("detail", "no snapshot found"))

    # --- Weather: always live (Open-Meteo, no key) ----------------------------
    weather_status = "live"
    weather_detail = "Open-Meteo (no key required)"

    # --- LLM: which provider/model the analyst will actually use -------------
    # Mirrors analyst._build_real_provider(): any real key engages the live
    # router (GROQ -> OPENROUTER -> GEMINI), independent of LLM_PROVIDER.
    groq_present = bool(os.getenv("GROQ_API_KEY", "").strip())
    openrouter_present = bool(os.getenv("OPENROUTER_API_KEY", "").strip())
    gemini_present = bool(os.getenv("GOOGLE_API_KEY", "").strip())
    configured_order = []
    if groq_present:
        configured_order.append("groq")
    if openrouter_present:
        configured_order.append("openrouter")
    if gemini_present:
        configured_order.append("gemini")

    if configured_order:
        llm_status = "enabled"
        llm_detail = "live LLM (" + " -> ".join(configured_order) + ")"
    else:
        llm_status = "template-fallback"
        llm_detail = "no LLM keys configured"

    # --- Phase 20.1: fixtures scan + BBC news radar states (materialized) -----
    from sqlalchemy import select  # noqa: PLC0415

    from fpl_intelligence.materialize.service import (  # noqa: PLC0415
        FIXTURES_MAX_AGE_SECONDS,
        NEWS_MAX_AGE_SECONDS,
    )
    from fpl_intelligence.sync.materialized_models import (  # noqa: PLC0415
        FixturesCacheDB,
        NewsCacheDB,
        PredictionCurrentDB,
    )
    from fpl_intelligence.sync.models import IngestedGameweekDB

    fx_row = db.scalar(
        select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()).limit(1)
    )
    if fx_row is not None:
        age_h = _age_seconds_since(fx_row.fetched_at) / 3600
        n_fix = len(fx_row.payload or [])
        if age_h * 3600 <= FIXTURES_MAX_AGE_SECONDS and n_fix:
            fixtures_status = "ok"
            fixtures_detail = (
                f"{n_fix} fixtures cached {age_h:.1f}h ago (source: {fx_row.source})"
            )
        else:
            fixtures_status = "stale"
            fixtures_detail = f"cache is {age_h:.0f}h old — waiting for cron"
    else:
        fixtures_status = "pending"
        fixtures_detail = "no cached fixtures yet — run /api/v1/admin/materialize"

    news_row = db.scalar(select(NewsCacheDB).order_by(NewsCacheDB.id.desc()).limit(1))
    news_matched: int | None = None
    if news_row is not None:
        age_min = _age_seconds_since(news_row.fetched_at) / 60
        if age_min * 60 <= NEWS_MAX_AGE_SECONDS and news_row.headline_count:
            bbc_status = "ok"
            bbc_detail = (
                f"{news_row.headline_count} headlines cached "
                f"{int(age_min)} min ago ({news_row.source})"
            )
            # Phase 22 (D5): how many players actually matched a headline.
            try:
                from fpl_intelligence.data_providers.bbc_news import (
                    NEWS_KEYWORDS,
                    match_headlines,
                )
                from fpl_intelligence.prediction.live_provider import load_player_catalog

                items = [i for i in (news_row.payload or []) if isinstance(i, dict)]
                catalog = load_player_catalog()
                player_rows = [
                    (
                        int(pid),
                        str(row.get("web_name") or ""),
                        str(row.get("first_name") or ""),
                        str(row.get("second_name") or ""),
                    )
                    for pid, row in catalog.items()
                ]
                flags = match_headlines(items, player_rows, NEWS_KEYWORDS)
                news_matched = len(flags)
                bbc_detail += f" · matched {news_matched} players"
            except Exception as exc:  # noqa: BLE001 — audit only
                logger.debug("news match audit failed: %s", exc)
        else:
            bbc_status = "stale"
            bbc_detail = f"last scan {int(age_min / 60)}h ago"
    else:
        bbc_status = "pending"
        bbc_detail = "no news scan yet — runs on squad pages"

    last_ingest = db.scalar(
        select(IngestedGameweekDB.ingested_at)
        .order_by(IngestedGameweekDB.ingested_at.desc())
        .limit(1)
    )
    ingested_gws = sorted(
        {
            int(gw)
            for (gw,) in db.execute(select(IngestedGameweekDB.gameweek).distinct()).all()
        }
    )
    if last_ingest is not None:
        ingest_age_h = _age_seconds_since(last_ingest) / 3600
        vaastav_status = "ok" if ingested_gws else "empty"
        gw_list = ", ".join(f"GW{gw}" for gw in ingested_gws[-3:])
        vaastav_detail = (
            f"{gw_list} results ingested, last {ingest_age_h:.1f}h ago"
            if ingested_gws
            else f"ingest ran but no GW rows yet ({ingest_age_h:.1f}h ago)"
        )
    else:
        vaastav_status = "pending"
        vaastav_detail = "no gameweek results ingested yet — daily 06:10 UTC cron"

    pred_rows = db.execute(
        select(PredictionCurrentDB.gameweek, func.count())
        .group_by(PredictionCurrentDB.gameweek)
        .order_by(PredictionCurrentDB.gameweek)
    ).all()
    pred_last = db.scalar(
        select(PredictionCurrentDB.computed_at)
        .order_by(PredictionCurrentDB.computed_at.desc())
        .limit(1)
    )
    if pred_rows:
        pred_age_h = (
            _age_seconds_since(pred_last) / 3600 if pred_last is not None else None
        )
        gw_txt = ",".join(f"GW{gw}({n})" for gw, n in pred_rows[:6])
        predictions_status = "ok" if (pred_age_h or 0) <= 36 else "stale"
        predictions_detail = f"xPTS for {gw_txt} — computed {pred_age_h:.1f}h ago"
    else:
        predictions_status = "pending"
        predictions_detail = "no precomputed xPTS yet — daily job runs at 06:10 UTC"

    # --- Phase 20.4: consolidated daily-job heartbeat --------------------------
    from fpl_intelligence.api.routes.admin import DAILY_JOB_NAME  # noqa: PLC0415
    from fpl_intelligence.db.models import IngestionRun  # noqa: PLC0415

    daily_row = db.scalar(
        select(IngestionRun)
        .where(IngestionRun.job_name == DAILY_JOB_NAME)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    )
    if daily_row is not None:
        daily_age_h = _age_seconds_since(daily_row.started_at) / 3600
        daily_ok = daily_row.status == "SUCCESS"
        daily_status = ("ok" if daily_ok else "degraded") if daily_age_h <= 30 else "stale"
        ran_txt = f"{int(daily_age_h)}h ago" if daily_age_h >= 1 else "recently"
        daily_detail = (
            f"last run {ran_txt} ({daily_row.status}) · "
            f"{daily_row.records_processed}/4 steps ok · schedule 06:10 UTC"
        )
    else:
        daily_status = "pending"
        daily_detail = "daily job has never run — schedule 06:10 UTC"

    # --- Phase 20.4: per-mask egress health (last status per strategy) ----------
    from fpl_intelligence.data_providers.fpl_egress import (  # noqa: PLC0415
        mask_health_payload,
    )

    mask_rows = mask_health_payload()

    payload = {
        "as_of": now,
        "sources": {
            "daily_job": {
                "status": daily_status,
                "detail": daily_detail,
            },
            "fixtures": {
                "status": fixtures_status,
                "detail": fixtures_detail,
            },
            "bbc_news": {
                "status": bbc_status,
                "detail": bbc_detail,
                "matched_players": news_matched,
            },
            "vaastav_results": {
                "status": vaastav_status,
                "detail": vaastav_detail,
            },
            "predictions_materialized": {
                "status": predictions_status,
                "detail": predictions_detail,
            },
            "fpl_import": {
                "status": fpl_status,
                "detail": fpl_detail + (f" · via {fpl_strategy}" if fpl_strategy else ""),
                "egress_strategy": fpl_strategy or "unprobed",
                "retry_schedule": "server-side fetch active — retried by the daily 06:10 cron",
            },
            "odds_api": {
                "status": odds_status,
                "detail": odds_detail,
            },
            "understat": {
                "status": understat_status,
                "detail": understat_detail,
            },
            "weather": {
                "status": weather_status,
                "detail": weather_detail,
            },
            "pl_photos": {
                "status": photos_status,
                "detail": photos_detail,
            },
            "llm": {
                "status": llm_status,
                "detail": llm_detail,
            },
        },
        "mask_health": mask_rows,
    }
    with _response_lock:
        globals()["_response_cache"] = (time.monotonic(), payload)
    return payload
