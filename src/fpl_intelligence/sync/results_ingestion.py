"""Phase 21.1 (T1) — official-FPL gameweek results ingestion.

When ``fixtures_cache`` shows a gameweek fully finished, the engine fetches
``GET /api/event/{gw}/live/`` through the egress mask chain and stores the
finalised per-element results (points / minutes / goals / assists / bonus)
as ingested history. That flips Track-Record recommendations from *pending*
to *graded* and fills the calibration ledger — no external push required.

vaastav remains a cross-check source: its CSVs feed the same
``ingested_history`` table via the daily materialize step, but the official
endpoint is authoritative for grading.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fpl_intelligence.sync.models import IngestedGameweekDB
from fpl_intelligence.sync.service import ingest_history_gameweek

logger = logging.getLogger(__name__)

#: A fully-ingested gameweek carries one row per Premier-League player (~600).
_MIN_ROWS_FOR_FULL_GW = 300


def _opt_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_event_live_payload(payload: Any) -> bool:
    """Egress-chain validator: an event/live payload has an ``elements`` list."""
    return isinstance(payload, dict) and isinstance(payload.get("elements"), list)


def parse_event_live(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise ``event/{gw}/live/`` elements into history rows."""
    rows: list[dict[str, Any]] = []
    for element in payload.get("elements") or []:
        if not isinstance(element, dict):
            continue
        try:
            element_id = int(element.get("id"))
        except (TypeError, ValueError):
            continue
        stats = element.get("stats") if isinstance(element.get("stats"), dict) else {}
        row: dict[str, Any] = {
            "element_id": element_id,
            "total_points": _opt_int(stats.get("total_points")) or 0,
        }
        for key in ("minutes", "bonus", "goals_scored", "assists"):
            value = _opt_int(stats.get(key))
            if value is not None:
                row[key] = value
        xgi = stats.get("expected_goal_involvements")
        if xgi not in (None, ""):
            row["xgi"] = _opt_float(xgi)
        rows.append(row)
    return rows


def finished_gameweeks_from_fixtures(fixtures: list[dict[str, Any]]) -> list[int]:
    """Gameweeks whose fixtures are ALL marked finished (pure, ascending)."""
    buckets: dict[int, dict[str, int]] = {}
    for item in fixtures or []:
        if not isinstance(item, dict):
            continue
        try:
            gw = int(item.get("event"))
        except (TypeError, ValueError):
            continue
        bucket = buckets.setdefault(gw, {"total": 0, "finished": 0})
        bucket["total"] += 1
        if item.get("finished"):
            bucket["finished"] += 1
    return sorted(gw for gw, b in buckets.items() if b["total"] > 0 and b["finished"] == b["total"])


async def fetch_event_live(
    gameweek: int,
    settings: Any = None,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Fetch one gameweek's live payload via the masks -> (rows, note).

    ``note`` names the winning mask on success or the failure reason; callers
    surface it verbatim so Sources stays honest about how data arrived.
    """
    try:
        from fpl_intelligence.config import get_settings  # noqa: PLC0415
        from fpl_intelligence.data_providers.fpl_egress import FplEgressChain  # noqa: PLC0415

        cfg = settings or get_settings()
        egress = FplEgressChain(
            cfg.fpl_base_url,
            timeout=cfg.egress_strategy_timeout,
            cache_ttl=0,  # results are final; never serve a stale half-time score
        )
        payload = await egress.fetch(
            f"/api/event/{int(gameweek)}/live/",
            validator=validate_event_live_payload,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as an honest failure
        return None, f"{type(exc).__name__}: {exc}"
    strategy = getattr(egress, "winning_strategy", None) or "direct"
    return parse_event_live(payload), strategy


def _gw_row_count(db: Session, gameweek: int) -> int:
    return int(
        db.scalar(
            select(func.count()).where(IngestedGameweekDB.gameweek == gameweek)
        )
        or 0
    )


def _gw_sources(db: Session, gameweek: int) -> set[str]:
    return {
        str(src)
        for (src,) in db.execute(
            select(IngestedGameweekDB.source).where(IngestedGameweekDB.gameweek == gameweek)
        ).all()
    }


async def ingest_finished_gameweeks(
    db: Session,
    *,
    force_gameweeks: tuple[int, ...] = (),
    max_gameweeks: int = 2,
) -> dict[str, Any]:
    """Ingest every finished-but-uningested gameweek found in fixtures_cache.

    Idempotent: a gameweek that already has full history rows (any source) is
    skipped unless listed in ``force_gameweeks`` (which re-fetches when the
    stored coverage is partial). Returns an honest per-gameweek report.
    """
    from fpl_intelligence.materialize.service import load_cached_fixtures  # noqa: PLC0415

    fixtures = load_cached_fixtures(db)
    candidates = finished_gameweeks_from_fixtures(fixtures)[-max_gameweeks:]
    report: dict[str, Any] = {"checked": list(candidates), "ingested": [], "skipped": []}

    targets: list[tuple[int, bool]] = []
    for gw in candidates:
        force = gw in force_gameweeks
        count = _gw_row_count(db, gw)
        already_full = count >= _MIN_ROWS_FOR_FULL_GW
        if already_full and not (force and count < _MIN_ROWS_FOR_FULL_GW * 2):
            srcs = ", ".join(sorted(_gw_sources(db, gw))) or "unknown"
            report["skipped"].append(
                {"gameweek": gw, "reason": f"already ingested ({count} rows via {srcs})"}
            )
            continue
        targets.append((gw, force))

    for forced_gw in force_gameweeks:
        if all(gw != forced_gw for gw, _ in targets):
            targets.append((forced_gw, True))

    for gw, _force in sorted(targets):
        rows, note = await fetch_event_live(gw)
        if not rows:
            logger.warning("event/%s/live fetch failed via %s", gw, note)
            report["skipped"].append({"gameweek": gw, "reason": f"fetch failed ({note})"})
            continue
        result = ingest_history_gameweek(db, gw, rows, source="fpl-live")
        db.commit()
        report["ingested"].append({"gameweek": gw, "via": note, **result})
        logger.info(
            "GW%s results ingested from official API via %s (%s rows)",
            gw,
            note,
            result.get("stored"),
        )

    report["ok"] = bool(report["ingested"]) or not targets
    return report
