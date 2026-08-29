"""Phase 25 Gate 0 (T1) — the transfer intelligence service.

Data flow (honesty contract: never invent a transfer):

1. ``official-history`` — ``GET /api/entry/{id}/history/`` through the egress
   mask chain. The payload's ``history[*].transfers`` array is parsed into
   ledger rows (gw, element_in, element_out, event_cost).
2. ``snapshot-diff (unofficial)`` — when every mask fails, consecutive
   :class:`SquadSnapshotDB` rows for the entry are diffed; players that left
   and entered between snapshots become one inferred transfer per gameweek.
3. Neither source available -> the API answers with an honest
   ``unavailable`` state and the UI shows an honest chip.

Horizon EV per row = Σ over materialized horizon GWs of (xPTS_in − xPTS_out)
− hit cost, computed ONLY from ``predictions_current`` rows that actually
exist. Missing predictions contribute nothing and are disclosed in the
"how computed" inputs.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.data_providers.registry import ProviderState
from fpl_intelligence.db.base import Base
from fpl_intelligence.sync.materialized_models import PredictionCurrentDB
from fpl_intelligence.transfers.models import SquadSnapshotDB, TransferLogDB

logger = logging.getLogger(__name__)

SOURCE_OFFICIAL = "official-history"
SOURCE_SNAPSHOT = "snapshot-diff (unofficial)"


def ensure_tables(db: Session) -> None:
    """Idempotent create for the two Phase 25 tables (any dialect)."""
    try:
        Base.metadata.create_all(
            db.get_bind(),
            tables=[TransferLogDB.__table__, SquadSnapshotDB.__table__],
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 — never fail a request on DDL
        db.rollback()
        logger.debug("transfer DDL skipped: %s", exc)


# --------------------------------------------------------------------------- #
# Official history fetch + parsing (pure helpers are unit-testable)
# --------------------------------------------------------------------------- #


def parse_history_transfers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten per-gameweek blocks into normalized rows (pure).

    The real ``/entry/{id}/history/`` payload is ``{current, past, chips}``
    where each ``current`` row carries ``event`` + counts (no element ids).
    A legacy ``history`` key whose blocks contain ``transfers`` arrays with
    ``element_in`` / ``element_out`` is honoured when present. Rows without
    both element ids are skipped rather than guessed.
    """
    out: list[dict[str, Any]] = []
    blocks: list[Any] = []
    if isinstance((payload or {}).get("history"), list):
        blocks = payload["history"]
    elif isinstance((payload or {}).get("current"), list):
        blocks = payload["current"]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        try:
            gw = int(block.get("event"))
        except (TypeError, ValueError):
            continue
        for tr in block.get("transfers") or []:
            if not isinstance(tr, dict):
                continue
            try:
                eid_in = int(tr.get("element_in"))
                eid_out = int(tr.get("element_out"))
            except (TypeError, ValueError):
                continue
            try:
                cost = int(tr.get("event_cost") or 0)
            except (TypeError, ValueError):
                cost = 0
            try:
                tid = int(tr["id"])
            except (TypeError, KeyError, ValueError):
                tid = None
            out.append(
                {
                    "gameweek": gw,
                    "transfer_id": tid,
                    "element_in": eid_in,
                    "element_out": eid_out,
                    "cost": max(0, cost),
                }
            )
    return out


async def fetch_official_transfers(
    entry_id: int, *, with_raw: bool = False
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """Fetch + parse the official history through the masks.

    Returns ``(rows, strategy_or_error, raw_excerpt)``; raises on total mask
    failure so callers can fall through to the snapshot diff honestly. The
    raw excerpt preserves the first non-empty transfers array verbatim for
    provenance display.
    """
    from fpl_intelligence.config import get_settings
    from fpl_intelligence.data_providers.registry import get_async_fpl_adapter

    def _validate(data: Any) -> None:
        if not isinstance(data, dict) or not (
            isinstance(data.get("history"), list)
            or isinstance(data.get("current"), list)
        ):
            raise ValueError(
                f"history payload missing 'current'/'history' list (got "
                f"{type(data).__name__})"
            )

    cfg = get_settings()
    result = await get_async_fpl_adapter(settings=cfg).resolve(
        f"/api/entry/{int(entry_id)}/history/", validator=_validate
    )
    if result.state is ProviderState.UNAVAILABLE:
        raise RuntimeError("FPL provider unavailable: " + "; ".join(result.errors))
    payload = result.value
    rows = parse_history_transfers(payload)
    excerpt: list[dict[str, Any]] = []
    if with_raw:
        # Verbatim newest event block (even when its array is empty) so the
        # UI/proof can prove official provenance, not just parsed rows.
        blocks: list[Any] = (
            payload["history"]
            if isinstance((payload or {}).get("history"), list)
            else (payload or {}).get("current")
            if isinstance((payload or {}).get("current"), list)
            else []
        )
        blocks = [b for b in blocks if isinstance(b, dict)]
        if blocks:
            last = max(blocks, key=lambda b: int(b.get("event") or 0))
            excerpt.append(
                {
                    "event": last.get("event"),
                    "event_transfers": last.get("event_transfers"),
                    "event_transfers_cost": last.get("event_transfers_cost"),
                    "transfers": (last.get("transfers") or [])[:5],
                }
            )
    return rows, result.provenance.get("egress_strategy", "direct"), excerpt


def snapshot_diff_rows(db: Session, entry_id: str) -> list[dict[str, Any]]:
    """Diff consecutive squad snapshots into inferred transfers (pure read).

    The newest snapshot wins as the target state; every player present in the
    previous snapshot but absent now is OUT, every new player is IN. Pairs are
    matched positionally (zip of outs/ins) which mirrors how managers swap
    players one-for-one; leftovers become single-sided rows with the missing
    side None (rendered as "—").
    """
    snaps = (
        db.execute(
            select(SquadSnapshotDB)
            .where(SquadSnapshotDB.entry_id == str(entry_id))
            .order_by(SquadSnapshotDB.captured_at.desc())
            .limit(2)
        )
        .scalars()
        .all()
    )
    if len(snaps) < 2:
        return []
    prev_ids = [int(p) for p in (snaps[1].player_ids or [])]
    curr_ids = [int(p) for p in (snaps[0].player_ids or [])]
    outs = [p for p in reversed(prev_ids) if p not in set(curr_ids)]
    ins = [p for p in curr_ids if p not in set(prev_ids)]
    if not outs and not ins:
        return []
    rows: list[dict[str, Any]] = []
    pairs = max(len(outs), len(ins))
    for i in range(pairs):
        eid_in = ins[i] if i < len(ins) else None
        eid_out = outs[i] if i < len(outs) else None
        if eid_in is None and eid_out is None:
            continue
        rows.append(
            {
                "gameweek": int(snaps[0].gameweek),
                "transfer_id": None,
                "element_in": eid_in,
                "element_out": eid_out,
                # A snapshot-diff cannot know whether a points hit was taken;
                # 0 means unknown-free, never invented.
                "cost": 0,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Horizon EV + persistence
# --------------------------------------------------------------------------- #


def _names_for(db: Session, ids: set[int]) -> dict[int, str]:
    from fpl_intelligence.prediction.live_provider import load_player_catalog

    catalog = load_player_catalog()
    names: dict[int, str] = {}
    for pid in ids:
        row = catalog.get(pid)
        names[pid] = str(row.get("web_name") or f"Player {pid}") if row else f"Player {pid}"
    try:
        from fpl_intelligence.db.models import Player

        if ids:
            for fid, name in db.execute(
                select(Player.fpl_element_id, Player.web_name).where(
                    Player.fpl_element_id.in_(ids)  # type: ignore[attr-defined]
                )
            ).all():
                if fid is not None:
                    names[int(fid)] = str(name)
    except Exception as exc:  # noqa: BLE001 — display-only enrichment
        logger.debug("player-name enrichment skipped: %s", exc)
    return names


def compute_horizon_ev(
    db: Session, rows: list[dict[str, Any]], *, start_gw: int | None = None
) -> list[dict[str, Any]]:
    """Attach ``horizon_ev`` to each row from materialized predictions only.

    Horizon = every gameweek >= start_gw present in ``predictions_current``
    for BOTH sides' coverage check (up to the newest stored week). A GW where
    either side lacks a prediction contributes 0 and is listed in ``gaps``
    so the UI can disclose it inside the "how computed" line.
    """
    xpts: dict[int, float] = {}
    gws: set[int] = set()
    try:
        result = db.execute(select(PredictionCurrentDB))
    except Exception as exc:  # noqa: BLE001 — table may be cold
        logger.warning("predictions_current unreadable for EV: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
        result = None
    if result is not None:
        for r in result.scalars().all():
            xpts[(int(r.gameweek), int(r.element_id))] = float(r.expected_points or 0.0)
            gws.add(int(r.gameweek))
    horizon = sorted(gw for gw in gws if start_gw is None or gw >= start_gw)

    enriched: list[dict[str, Any]] = []
    for row in rows:
        eid_in = row.get("element_in")
        eid_out = row.get("element_out")
        ev = 0.0
        used: list[int] = []
        gaps: list[int] = []
        for gw in horizon:
            p_in = xpts.get((gw, int(eid_in))) if eid_in else 0.0
            p_out = xpts.get((gw, int(eid_out))) if eid_out else 0.0
            has_in = (gw, int(eid_in)) in xpts if eid_in else False
            has_out = (gw, int(eid_out)) in xpts if eid_out else False
            if has_in != has_out or (not has_in and not has_out):
                gaps.append(gw)
                continue
            ev += p_in - p_out
            used.append(gw)
        ev -= float(row.get("cost") or 0)
        enriched.append({**row, "horizon_ev": round(ev, 2), "horizon_gws": used, "ev_gaps": gaps})
    return enriched


def persist_ledger(
    db: Session, entry_id: str, rows: list[dict[str, Any]], source: str
) -> int:
    """Idempotent upsert of ledger rows; returns rows written."""
    now = datetime.now(UTC)
    written = 0
    for row in rows:
        tid = row.get("transfer_id")
        existing = None
        q = select(TransferLogDB).where(
            TransferLogDB.entry_id == str(entry_id),
            TransferLogDB.gameweek == int(row["gameweek"]),
            TransferLogDB.element_in == (row.get("element_in") or -1),
            TransferLogDB.element_out == (row.get("element_out") or -1),
        )
        if tid is not None:
            q2 = q.where(TransferLogDB.transfer_id == int(tid))
            existing = db.scalar(q2) or db.scalar(q)
        else:
            existing = db.scalar(q)
        if existing is not None:
            existing.cost = int(row.get("cost") or 0)
            existing.source = source
            continue
        # Pass 2 dedupe: snapshot diffs can infer the SAME swap twice with the
        # in/out sides flipped (live bug: "Cash(#32) ↔ De Cuyper(#115) swap
        # listed twice"). Before inserting, look for an existing same-gameweek
        # row with a NULL transfer_id and the REVERSED element pair; when found,
        # UPDATE it to the newest inference instead of inserting a duplicate.
        if tid is None and row.get("element_in") is not None and row.get("element_out") is not None:
            reversed_row = db.scalar(
                select(TransferLogDB).where(
                    TransferLogDB.entry_id == str(entry_id),
                    TransferLogDB.gameweek == int(row["gameweek"]),
                    TransferLogDB.transfer_id.is_(None),
                    TransferLogDB.element_in == row["element_out"],
                    TransferLogDB.element_out == row["element_in"],
                )
            )
            if reversed_row is not None:
                reversed_row.element_in = row["element_in"]
                reversed_row.element_out = row["element_out"]
                reversed_row.name_in = row.get("name_in")
                reversed_row.name_out = row.get("name_out")
                reversed_row.cost = int(row.get("cost") or 0)
                reversed_row.source = source
                continue
        db.add(
            TransferLogDB(
                entry_id=str(entry_id),
                gameweek=int(row["gameweek"]),
                transfer_id=tid,
                element_in=row.get("element_in"),
                element_out=row.get("element_out"),
                name_in=row.get("name_in"),
                name_out=row.get("name_out"),
                cost=int(row.get("cost") or 0),
                source=source,
                created_at=now,
            )
        )
        written += 1
    db.commit()
    return written


async def build_ledger(db: Session, entry_id: str | int) -> dict[str, Any]:
    """Full ledger payload for one entry: official first, snapshot fallback."""
    ensure_tables(db)
    eid = str(entry_id)
    status = "ok"
    note = ""
    rows: list[dict[str, Any]] = []
    source = SOURCE_OFFICIAL
    strategy: str | None = None
    raw_excerpt: list[dict[str, Any]] = []
    try:
        rows, strategy, raw_excerpt = await fetch_official_transfers(
            int(entry_id), with_raw=True
        )
    except Exception as exc:  # noqa: BLE001 — honest fallback below
        logger.info("official history unavailable for %s: %s", eid, type(exc).__name__)
        rows = snapshot_diff_rows(db, eid)
        source = SOURCE_SNAPSHOT
        strategy = None
        if rows:
            status = "fallback"
            note = (
                "Official history unreachable — transfers inferred by diffing "
                "consecutive squad snapshots (unofficial)."
            )
        else:
            status = "unavailable"
            note = (
                "Transfer history unavailable: official FPL history could not "
                "be fetched and no squad snapshots exist yet."
            )

    if rows:
        names = _names_for(db, {r["element_in"] for r in rows if r.get("element_in")}
                           | {r["element_out"] for r in rows if r.get("element_out")})
        for r in rows:
            r["name_in"] = names.get(r.get("element_in")) if r.get("element_in") else None
            r["name_out"] = names.get(r.get("element_out")) if r.get("element_out") else None
        persist_ledger(db, eid, rows, source)
    

    try:
        stored = (
            db.execute(
                select(TransferLogDB)
                .where(TransferLogDB.entry_id == eid)
                .order_by(TransferLogDB.gameweek.desc(), TransferLogDB.id.desc())
            )
            .scalars()
            .all()
        )
        ledger = [
            {
                "gameweek": r.gameweek,
                "element_in": r.element_in,
                "element_out": r.element_out,
                "name_in": r.name_in,
                "name_out": r.name_out,
                "cost": r.cost,
                "source": r.source,
            }
            for r in stored
        ]
    except Exception as exc:  # noqa: BLE001 — cold table -> honest state
        logger.warning("transfer_log unreadable: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return {
            "entry_id": eid,
            "status": "unavailable",
            "note": "Transfer ledger storage is not initialised yet.",
            "source": None,
            "strategy": None,
            "transfers": [],
            "count": 0,
            "generated_at": datetime.now(UTC).isoformat(),
        }
    # Recompute EV over the persisted view too so the response is complete.
    enriched_all = compute_horizon_ev(db, ledger, start_gw=None)
    ev_by_key = {
        (r["gameweek"], r.get("element_in"), r.get("element_out")): r
        for r in enriched_all
    }
    for row in ledger:
        hit = ev_by_key.get((row["gameweek"], row.get("element_in"), row.get("element_out")))
        row["horizon_ev"] = hit.get("horizon_ev") if hit else None
        row["how_computed"] = (
            "horizon EV = Σ(xPTS_in − xPTS_out) over materialized prediction "
            "GWs − hit cost; missing prediction GWs excluded"
        )

    return {
        "entry_id": eid,
        "status": status,
        "note": note,
        "source": source if rows else None,
        "strategy": strategy,
        "history_excerpt": raw_excerpt,
        "transfers": ledger,
        "count": len(ledger),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def capture_snapshot(
    db: Session, entry_id: str | int, player_ids: list[int], gameweek: int, bank: float | None
) -> bool:
    """Store a squad snapshot when the roster changed; True when new state.

    Called on every squad save path. Consecutive identical rosters do NOT
    create rows (the diff fallback would produce empty results anyway).
    """
    ensure_tables(db)
    latest = db.scalar(
        select(SquadSnapshotDB)
        .where(SquadSnapshotDB.entry_id == str(entry_id))
        .order_by(SquadSnapshotDB.captured_at.desc())
        .limit(1)
    )
    if latest is not None and [int(p) for p in (latest.player_ids or [])] == [
        int(p) for p in player_ids
    ]:
        return False
    db.add(
        SquadSnapshotDB(
            entry_id=str(entry_id),
            gameweek=int(gameweek),
            player_ids=[int(p) for p in player_ids],
            bank=bank,
            captured_at=datetime.now(UTC),
        )
    )
    db.commit()
    return True


def detect_transfer_between_snapshots(db: Session, entry_id: str | int) -> dict[str, Any] | None:
    """Newest inferred transfer between the two latest snapshots (banner)."""
    rows = snapshot_diff_rows(db, str(entry_id))
    if not rows:
        return None
    row = rows[0]
    ids: set[int] = set()
    for key in ("element_in", "element_out"):
        if row.get(key):
            ids.add(int(row[key]))
    names = _names_for(db, ids) if ids else {}
    return {
        "element_in": row.get("element_in"),
        "element_out": row.get("element_out"),
        "name_in": names.get(row.get("element_in")),
        "name_out": names.get(row.get("element_out")),
        "gameweek": row["gameweek"],
        "source": SOURCE_SNAPSHOT,
    }
