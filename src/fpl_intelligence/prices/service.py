"""Phase 23 Gate 1 (L3) — the price engine.

Daily now_cost diffs become ``price_moves`` rows plus a full
``price_snapshots`` history. Everything downstream (▲/▼ chips on squad and
decisions cards, the "Today's risers/fallers" top-5 strip, the Friday-brief
price section and the prices push trigger) reads from these two tables.

Pure diff logic lives in :func:`detect_moves` so tests never need a DB.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from datetime import date as date_cls
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Pure math
# --------------------------------------------------------------------------- #


def detect_moves(
    today_map: dict[int, int],
    yesterday_map: dict[int, int],
) -> list[dict[str, Any]]:
    """Elements whose now_cost changed between two snapshots (pure).

    Returns ``[{element_id, old_cost, new_cost, delta}]`` with delta in £0.1m
    units (positive = riser). Elements missing from either side are skipped —
    no invented zeros.
    """
    moves: list[dict[str, Any]] = []
    for element_id, new_cost in today_map.items():
        old = yesterday_map.get(int(element_id))
        if old is None or old == int(new_cost):
            continue
        moves.append(
            {
                "element_id": int(element_id),
                "old_cost": float(old),
                "new_cost": float(new_cost),
                "delta": int(new_cost) - int(old),
            }
        )
    moves.sort(key=lambda m: (-abs(m["delta"]), m["element_id"]))
    return moves


def price_label(cost_10m: float | None) -> str:
    """£5.6m style label from a 0.1m-unit cost."""
    if cost_10m is None:
        return ""
    return f"£{float(cost_10m) / 10:.1f}m"


# --------------------------------------------------------------------------- #
# DB orchestration
# --------------------------------------------------------------------------- #


def ensure_price_tables(db: Any) -> None:
    """Self-sealing DDL for prod DBs predating Phase 23."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    ddl = (
        """
        CREATE TABLE IF NOT EXISTS price_moves (
            id SERIAL PRIMARY KEY,
            element_id INTEGER NOT NULL,
            gameweek INTEGER NOT NULL,
            old_cost DOUBLE PRECISION,
            new_cost DOUBLE PRECISION,
            delta INTEGER NOT NULL DEFAULT 0,
            moved_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT uq_price_move UNIQUE (gameweek, element_id, delta)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS price_snapshots (
            id SERIAL PRIMARY KEY,
            snapshot_date DATE NOT NULL,
            element_id INTEGER NOT NULL,
            now_cost INTEGER NOT NULL DEFAULT 0,
            CONSTRAINT uq_price_snapshot UNIQUE (snapshot_date, element_id)
        )
        """,
    )
    insp = sa_inspect(db.get_bind())
    try:
        for statement in ddl:
            table = statement.split("EXISTS")[1].split("(")[0].strip()
            if insp.has_table(table):
                continue
            db.execute(text(statement))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — sqlite tests pre-create tables
        db.rollback()
        logger.debug("price DDL skipped: %s", exc)


def snapshot_prices(
    db: Any,
    facts: dict[int, dict[str, Any]],
    *,
    today: date_cls | None = None,
) -> int:
    """Upsert today's per-element now_cost snapshots; returns rows written."""
    from sqlalchemy import select

    from fpl_intelligence.prices.models import PriceSnapshotDB

    day = today or datetime.now(UTC).date()
    written = 0
    for element_id, fact in facts.items():
        cost = fact.get("now_cost")
        if cost is None:
            continue
        existing = db.scalar(
            select(PriceSnapshotDB).where(
                PriceSnapshotDB.snapshot_date == day,
                PriceSnapshotDB.element_id == int(element_id),
            )
        )
        if existing is None:
            db.add(
                PriceSnapshotDB(
                    snapshot_date=day,
                    element_id=int(element_id),
                    now_cost=int(cost),
                )
            )
        else:
            existing.now_cost = int(cost)
        written += 1
    db.commit()
    return written


def latest_snapshot_dates(db: Any) -> tuple[date_cls | None, date_cls | None]:
    """(latest, previous) distinct snapshot dates, newest first."""
    from sqlalchemy import distinct, select

    from fpl_intelligence.prices.models import PriceSnapshotDB

    days = [
        d[0]
        for d in db.execute(select(distinct(PriceSnapshotDB.snapshot_date)))
        .all()
    ]
    days = sorted(days, reverse=True)
    if not days:
        return None, None
    prev = days[1] if len(days) > 1 else None
    return days[0], prev


def record_price_moves(db: Any, gameweek: int) -> int:
    """Diff today's snapshot vs the previous one; persist new price_moves."""
    from sqlalchemy import select

    from fpl_intelligence.prices.models import PriceMoveDB, PriceSnapshotDB

    latest_day, previous_day = latest_snapshot_dates(db)
    if latest_day is None or previous_day is None:
        logger.info("price engine needs two snapshot days before moves appear")
        return 0

    def _map(day: date_cls) -> dict[int, int]:
        rows = db.execute(
            select(PriceSnapshotDB.element_id, PriceSnapshotDB.now_cost).where(
                PriceSnapshotDB.snapshot_date == day
            )
        ).all()
        return {int(e): int(c) for e, c in rows}

    moves = detect_moves(_map(latest_day), _map(previous_day))
    stored = 0
    now = datetime.now(UTC)
    for mv in moves:
        exists = db.scalar(
            select(PriceMoveDB.id).where(
                PriceMoveDB.gameweek == int(gameweek),
                PriceMoveDB.element_id == mv["element_id"],
                PriceMoveDB.delta == mv["delta"],
            )
        )
        if exists is not None:
            continue
        db.add(
            PriceMoveDB(
                element_id=mv["element_id"],
                gameweek=int(gameweek),
                old_cost=mv["old_cost"],
                new_cost=mv["new_cost"],
                delta=mv["delta"],
                moved_at=now,
            )
        )
        stored += 1
    db.commit()
    return stored


def _name_lookup(db: Any) -> dict[int, str]:
    names: dict[int, str] = {}
    try:
        from sqlalchemy import select as sel

        from fpl_intelligence.sync.materialized_models import ElementFactDB

        for eid, web in db.execute(
            sel(ElementFactDB.element_id, ElementFactDB.web_name)
        ).all():
            if web:
                names[int(eid)] = str(web)
    except Exception:  # noqa: BLE001 — display-only fallback
        pass
    if len(names) >= 100:
        return names
    try:
        from fpl_intelligence.prediction.live_provider import load_player_catalog

        for pid, row in load_player_catalog().items():
            name = str(row.get("web_name") or "")
            if name and int(pid) not in names:
                names[int(pid)] = name
    except Exception:  # noqa: BLE001 — display-only fallback
        pass
    return names


def todays_moves_payload(
    db: Any,
    *,
    limit: int = 5,
    gameweek: int | None = None,
) -> dict[str, Any]:
    """Top-N risers/fallers strip + honest empty state."""
    from sqlalchemy import select

    from fpl_intelligence.prices.models import PriceMoveDB

    stmt = (
        select(PriceMoveDB)
        .order_by(PriceMoveDB.moved_at.desc(), PriceMoveDB.id.desc())
        .limit(400)
    )
    rows = db.execute(stmt).scalars().all()
    if gameweek is not None:
        gw_rows = [r for r in rows if r.gameweek == int(gameweek)]
        rows = gw_rows or rows
    names = _name_lookup(db)

    def _card(r: PriceMoveDB) -> dict[str, Any]:
        sign = "+" if r.delta > 0 else ""
        return {
            "element_id": r.element_id,
            "web_name": names.get(r.element_id, f"Player {r.element_id}"),
            "delta": r.delta,
            "label": f"{sign}{r.delta / 10:.1f}",
            "new_cost": price_label(r.new_cost),
            "gameweek": r.gameweek,
        }

    risers = [_card(r) for r in rows if r.delta > 0][:limit]
    fallers = [_card(r) for r in rows if r.delta < 0][:limit]
    return {
        "risers": risers,
        "fallers": fallers,
        "has_data": bool(risers or fallers),
        "note": None if (risers or fallers)
        else "No price moves recorded yet — the daily job builds the history "
             "after its second run.",
    }


def todays_moves_note(db: Any) -> str:
    """One-line summary for the Friday prices push ("▲2 ▼3 · top riser ...")."""
    payload = todays_moves_payload(db, limit=1)
    risers, fallers = payload["risers"], payload["fallers"]
    top = (risers or fallers or [{}])[0]
    head = (
        f"top mover {top.get('web_name', '?')} {top.get('label', '')}"
        if top.get("web_name")
        else "quiet day"
    )
    return f"Price moves: ▲{len(risers)} ▼{len(fallers)} · {head} — full strip on Decisions."


def price_chip_map(db: Any, player_ids: list[int]) -> dict[int, int]:
    """Latest known delta per element (for ▲/▼ chips); absent = flat."""
    from sqlalchemy import select

    from fpl_intelligence.prices.models import PriceMoveDB

    wanted = {int(p) for p in player_ids}
    if not wanted:
        return {}
    rows = db.execute(
        select(PriceMoveDB)
        .where(PriceMoveDB.element_id.in_(wanted))
        .order_by(PriceMoveDB.moved_at.desc(), PriceMoveDB.id.desc())
        .limit(200)
    ).scalars().all()
    chips: dict[int, int] = {}
    for r in rows:
        chips.setdefault(int(r.element_id), int(r.delta))
    return chips
