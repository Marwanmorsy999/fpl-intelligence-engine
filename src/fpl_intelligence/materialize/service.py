"""Phase 20.1 — materialization service (the single daily writer).

``materialize_all`` is the ONLY writer of the four read-model tables:

1. vaastav GW results      -> ``ingested_history``   (idempotent upsert)
2. vaastav fixtures.csv    -> ``fixtures_cache``     (single row, newest wins)
3. BBC Sport RSS direct    -> ``news_cache``         (single row)
4. vaastav players_raw.csv -> ``element_facts``      (upsert per element)
5. prediction chain        -> ``predictions_current``(next 5 GWs, all players)

Everything runs inside the 06:10 cron where live network calls are allowed.
The request paths then serve from these tables with zero egress.

Read helpers used by the API layer:

* :func:`load_cached_fixtures`  — fixtures payload within a max age
* :func:`load_cached_news_items`— BBC items within a max age
* :func:`team_names_from_db`    — official team id -> short name map
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fpl_intelligence.data_providers.bbc_news import parse_feed
from fpl_intelligence.db.models import Team, TeamExternalId
from fpl_intelligence.fixtures.scanner import infer_current_gameweek, parse_fixtures
from fpl_intelligence.materialize.vaastav import (
    fetch_text,
    fixtures_url,
    gw_url,
    parse_fixtures_csv,
    parse_gw_results_csv,
    parse_players_raw_csv,
    players_raw_url,
)
from fpl_intelligence.sync.materialized_models import (
    ElementFactDB,
    FixturesCacheDB,
    NewsCacheDB,
    PredictionCurrentDB,
)
from fpl_intelligence.sync.models import IngestedGameweekDB

logger = logging.getLogger(__name__)

#: Season the incident fix targets (matches admin.SEASON_CODE).
SEASON_CODE = "2026-27"

BBC_RSS_URL = "https://feeds.bbci.co.uk/sport/football/rss.xml"

#: How stale a cached payload may be before request paths fall back to
#: inline behaviour. Generous: the daily cron refreshes everything anyway.
FIXTURES_MAX_AGE_SECONDS = 7 * 86400.0
NEWS_MAX_AGE_SECONDS = 3 * 86400.0
PREDICTIONS_MAX_AGE_SECONDS = 36 * 3600.0

#: GW-results window ingested each run (current + two previous).
RESULTS_WINDOW = 3


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Step 1 — vaastav gameweek results -> ingested_history
# --------------------------------------------------------------------------- #
async def ingest_vaastav_results(db: Session, season_code: str) -> dict[str, Any]:
    """Upsert last-N gameweek results; tolerate not-yet-published GWs."""
    existing_gws = {
        int(gw)
        for (gw,) in db.execute(select(IngestedGameweekDB.gameweek).distinct()).all()
    }

    # Determine candidate window from whatever fixtures we can see first.
    fixtures_row = db.scalar(select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()))
    if fixtures_row is not None and fixtures_row.payload:
        current_gw = infer_current_gameweek(parse_fixtures(fixtures_row.payload))
    else:
        current_gw = 1

    inserted = updated = skipped = 0
    fetched_gws: list[int] = []
    now = _now()
    for gw in range(max(1, current_gw - (RESULTS_WINDOW - 1)), current_gw + 1):
        text = await fetch_text(gw_url(season_code, gw))
        if text is None:
            skipped += 1
            continue
        fetched_gws.append(gw)
        rows = parse_gw_results_csv(text)
        for parsed in rows:
            element_id = int(parsed["element_id"])
            existing = db.scalar(
                select(IngestedGameweekDB).where(
                    IngestedGameweekDB.gameweek == gw,
                    IngestedGameweekDB.element_id == element_id,
                )
            )
            if existing is None:
                db.add(
                    IngestedGameweekDB(
                        gameweek=gw,
                        element_id=element_id,
                        source=f"vaastav:{season_code}",
                        total_points=int(parsed["total_points"]),
                        minutes=parsed["minutes"],
                        bonus=parsed["bonus"],
                        goals_scored=parsed["goals_scored"],
                        assists=parsed["assists"],
                        payload=parsed["payload"],
                        ingested_at=now,
                    )
                )
                inserted += 1
            else:
                existing.total_points = int(parsed["total_points"])
                existing.minutes = parsed["minutes"]
                existing.bonus = parsed["bonus"]
                existing.goals_scored = parsed["goals_scored"]
                existing.assists = parsed["assists"]
                existing.payload = parsed["payload"]
                existing.source = f"vaastav:{season_code}"
                existing.ingested_at = now
                updated += 1
        db.flush()
    db.commit()
    return {
        "fetched_gameweeks": fetched_gws,
        "skipped_unpublished": skipped,
        "rows_inserted": inserted,
        "rows_updated": updated,
        "total_ingested_gws": len(existing_gws | set(fetched_gws)),
    }


# --------------------------------------------------------------------------- #
# Step 2 — vaastav fixtures.csv -> fixtures_cache
# --------------------------------------------------------------------------- #
async def refresh_fixtures_cache(db: Session, season_code: str) -> dict[str, Any]:
    """Replace the fixtures cache with the freshest vaastav payload."""
    text = await fetch_text(fixtures_url(season_code))
    if text is None:
        return {"ok": False, "reason": "fetch failed / not published"}
    payload = parse_fixtures_csv(text)
    if not payload:
        return {"ok": False, "reason": "empty fixture csv"}
    db.execute(delete(FixturesCacheDB))
    db.add(
        FixturesCacheDB(
            source=f"vaastav:{season_code}",
            payload=payload,
            fetched_at=_now(),
        )
    )
    db.commit()
    upcoming = sorted({r["event"] for r in payload if not r["finished"]})
    return {
        "ok": True,
        "fixtures": len(payload),
        "next_unfinished_gw": upcoming[0] if upcoming else None,
    }


# --------------------------------------------------------------------------- #
# Step 3 — BBC RSS direct -> news_cache
# --------------------------------------------------------------------------- #
async def refresh_news_cache(db: Session) -> dict[str, Any]:
    """Fetch BBC football headlines directly and cache them."""
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": "fpl-intelligence-engine/20.1"},
            follow_redirects=True,
        ) as client:
            response = await client.get(BBC_RSS_URL)
        xml_text = response.text if response.status_code == 200 else ""
    except Exception as exc:  # noqa: BLE001 — news is best-effort
        logger.warning("bbc rss fetch failed: %s", exc)
        xml_text = ""

    items = parse_feed(xml_text) if xml_text else []
    serialized = [
        {"title": item.title, "link": item.link, "published": item.published}
        for item in items
    ]
    db.execute(delete(NewsCacheDB))
    db.add(
        NewsCacheDB(
            source="bbc-rss",
            headline_count=len(serialized),
            payload=serialized,
            fetched_at=_now(),
        )
    )
    db.commit()
    return {"ok": bool(serialized), "headlines": len(serialized)}


# --------------------------------------------------------------------------- #
# Step 4 — players_raw.csv -> element_facts
# --------------------------------------------------------------------------- #
async def refresh_element_facts(db: Session, season_code: str) -> dict[str, Any]:
    """Upsert bootstrap-style facts for every element."""
    text = await fetch_text(players_raw_url(season_code))
    if text is None:
        return {"ok": False, "reason": "fetch failed"}
    facts = parse_players_raw_csv(text)
    now = _now()
    upserted = 0
    for element_id, fact in facts.items():
        row = db.get(ElementFactDB, int(element_id))
        if row is None:
            row = ElementFactDB(element_id=int(element_id), updated_at=now)
            db.add(row)
        row.web_name = fact["web_name"] or row.web_name
        row.team_id = fact["team_id"]
        row.minutes = fact["minutes"]
        row.selected_by_percent = fact["selected_by_percent"]
        row.cost_change_event = fact["cost_change_event"]
        row.status = fact["status"]
        row.news = fact["news"]
        row.updated_at = now
        upserted += 1
    db.commit()
    return {"ok": True, "elements": upserted}


# --------------------------------------------------------------------------- #
# Step 5 — prediction chain -> predictions_current (next 5 GWs)
# --------------------------------------------------------------------------- #
async def precompute_predictions(
    db: Session, *, horizon: int = 5, base_gameweek: int | None = None
) -> dict[str, Any]:
    """Run the full chain once per upcoming GW and persist every player.

    This is deliberately the expensive path: it may hit odds/weather/understat
    enrichment exactly ONCE per day. The request paths never do.

    ``base_gameweek`` (Phase 21.1 T2) pins the horizon to the official FPL
    next-deadline gameweek so ``predictions_current`` covers exactly what the
    request paths will ask for; when omitted the fixtures-cache inference is
    used as before.
    """
    from fastapi.concurrency import run_in_threadpool

    from fpl_intelligence.api.deps import get_prediction_provider

    fixtures_row = db.scalar(select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()))
    if fixtures_row is None or not fixtures_row.payload:
        return {"ok": False, "reason": "no fixtures cache yet"}

    current_gw = infer_current_gameweek(parse_fixtures(fixtures_row.payload))
    if base_gameweek is not None:
        current_gw = max(current_gw, int(base_gameweek))
    provider = get_prediction_provider(db)

    total_rows = 0
    gws_done: list[int] = []
    errors: dict[int, str] = {}
    now = _now()

    def _resolve_and_store(gw: int) -> int:
        # Phase 21.1: skip the materialized fast-path while precomputing —
        # otherwise a bad run's zeros get re-served and re-written forever.
        try:
            preds = provider.get_all_predictions(gw, skip_materialized=True)
        except TypeError:
            preds = provider.get_all_predictions(gw)
        if not preds:
            raise RuntimeError("chain produced no predictions")
        db.execute(
            delete(PredictionCurrentDB).where(PredictionCurrentDB.gameweek == gw)
        )
        for pid, pred in preds.items():
            xg = xa = None
            breakdown = getattr(pred, "breakdown", None)
            db.add(
                PredictionCurrentDB(
                    gameweek=gw,
                    element_id=int(pid),
                    expected_points=float(pred.expected_points),
                    minutes_estimate=(
                        float(pred.expected_minutes)
                        if pred.expected_minutes is not None
                        else None
                    ),
                    start_prob=(
                        float(pred.start_probability)
                        if pred.start_probability is not None
                        else None
                    ),
                    xg_per_90=xg,
                    xa_per_90=xa,
                    source=getattr(pred, "source", None),
                    data_quality=getattr(pred, "data_quality", None),
                    breakdown=(
                        {k: round(float(v), 3) for k, v in breakdown.items()}
                        if isinstance(breakdown, dict)
                        else None
                    ),
                    computed_at=now,
                )
            )
        db.commit()
        return len(preds)

    for offset in range(horizon):
        gw = current_gw + offset
        try:
            count = await run_in_threadpool(_resolve_and_store, gw)
            gws_done.append(gw)
            total_rows += count
        except Exception as exc:  # noqa: BLE001 — one GW failing must not kill the rest
            errors[gw] = f"{type(exc).__name__}: {exc}"
            db.rollback()
            logger.warning("precompute gw=%s failed: %s", gw, exc)

    return {
        "ok": bool(gws_done),
        "gameweeks": gws_done,
        "rows": total_rows,
        "errors": errors,
        "base_gameweek": current_gw,
    }


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
async def materialize_all(
    db: Session, *, season_code: str = SEASON_CODE, base_gameweek: int | None = None
) -> dict[str, Any]:
    """Run every materialization step and return a combined report."""
    started = time.perf_counter()
    report: dict[str, Any] = {"season_code": season_code}

    report["fixtures"] = await refresh_fixtures_cache(db, season_code)
    report["results"] = await ingest_vaastav_results(db, season_code)
    report["news"] = await refresh_news_cache(db)
    report["element_facts"] = await refresh_element_facts(db, season_code)
    report["predictions"] = await precompute_predictions(db, base_gameweek=base_gameweek)
    report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    report["ran_at"] = _now().isoformat()
    return report


# --------------------------------------------------------------------------- #
# Read helpers used by the request path (indexed queries only)
# --------------------------------------------------------------------------- #
def load_cached_fixtures(db: Session) -> list[dict[str, Any]]:
    """Fresh-enough raw fixtures payload, or ``[]`` when absent."""
    row = db.scalar(
        select(FixturesCacheDB)
        .where(
            FixturesCacheDB.fetched_at
            >= _now() - timedelta(seconds=FIXTURES_MAX_AGE_SECONDS)
        )
        .order_by(FixturesCacheDB.id.desc())
    )
    if row is None or not isinstance(row.payload, list):
        return []
    return [item for item in row.payload if isinstance(item, dict)]


def load_cached_news_items(db: Session) -> tuple[list[dict[str, Any]], datetime | None]:
    """Fresh-enough BBC items plus their fetch timestamp."""
    row = db.scalar(
        select(NewsCacheDB)
        .where(
            NewsCacheDB.fetched_at >= _now() - timedelta(seconds=NEWS_MAX_AGE_SECONDS)
        )
        .order_by(NewsCacheDB.id.desc())
    )
    if row is None or not isinstance(row.payload, list):
        return [], None
    items = [item for item in row.payload if isinstance(item, dict)]
    return items, row.fetched_at


def team_names_from_db(db: Session) -> dict[int, str]:
    """Official FPL team id -> short name, joined through the external-id map.

    Replaces the stale hardcoded 2025/26 ``TEAM_SHORT_NAMES`` map that caused
    the Phase 20.1 fixtures incident (team ids reshuffled for 2026/27).
    """
    rows = db.execute(
        select(TeamExternalId.provider_team_id, Team.short_name, Team.name)
        .join(Team, Team.id == TeamExternalId.team_id)
        .where(TeamExternalId.provider == "official_fpl")
    ).all()
    names: dict[int, str] = {}
    for provider_id, short_name, name in rows:
        key = int(provider_id)
        names[key] = short_name or (name[:3].upper() if name else f"T{key}")
    return names
