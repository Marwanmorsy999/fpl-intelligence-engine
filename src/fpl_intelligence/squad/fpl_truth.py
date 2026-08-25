"""v2.6.0-sync-final â€” single source of FPL truth for fpl-view and sync.

Every consumer that needs "what does FPL actually show right now" goes through
:func:`fetch_fpl_truth` so the dashboard card, the sync save path and the
banner can never disagree. It fetches, through the egress mask chain:

* ``/api/entry/{id}/``            -> entry + ``current_event``
* ``/api/bootstrap-static/``      -> events (next-GW resolution)
* ``/api/entry/{id}/history/``    -> official per-GW transfer counts
* ``/api/entry/{id}/event/N/picks`` for current + next GW (status classified)
* ``/api/entry/{id}/transfers/``  -> confirmed transfers (mid-window quirk:
  picks may 404 while transfers are already recorded)

Nothing here mutates state; it is a read-only lens.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from fpl_intelligence.squad.fpl_import import FplSquadImporter

logger = logging.getLogger(__name__)

HISTORY_PATH_FMT = "/api/entry/{entry_id}/history/"
TRANSFERS_PATH_FMT = "/api/entry/{entry_id}/transfers/"


@dataclass
class FplTruth:
    """Read-only snapshot of what FPL shows for one entry."""

    current_event: int
    next_gw: int | None
    picks_current_ids: list[int] = field(default_factory=list)
    picks_current_status: int = 0
    picks_next_ids: list[int] = field(default_factory=list)
    picks_next_status: int = 404
    #: Official history row for ``next_gw`` when FPL has written one.
    history_row: dict[str, Any] | None = None
    #: Newest history row that exists (any GW).
    latest_history_row: dict[str, Any] | None = None
    #: Confirmed transfers (element_in/out) targeting ``next_gw``, merged from
    #: the transfers endpoint and the history payload's transfers arrays.
    next_gw_transfers: list[dict[str, Any]] = field(default_factory=list)
    entry_name: str | None = None

    # -- derived helpers -----------------------------------------------------

    @property
    def next_transfers_count(self) -> int:
        """Confirmed transfer count for the target GW (deduped by ids)."""
        seen: set[tuple[int, int]] = set()
        for tr in self.next_gw_transfers:
            key = (int(tr.get("element_in") or 0), int(tr.get("element_out") or 0))
            seen.add(key)
        return len(seen)

    def history_event_transfers(self) -> int | None:
        """``event_transfers`` from the target-GW history row, else ``None``."""
        if not self.history_row:
            return None
        raw = self.history_row.get("event_transfers")
        if isinstance(raw, (int, str)):
            try:
                return int(raw)
            except ValueError:
                return None
        return None


async def _fetch_history(
    importer: FplSquadImporter, entry_id: int
) -> list[dict[str, Any]]:
    """Best-effort fetch of ``history[*]`` rows; empty on any failure."""

    def _validate(data: Any) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("history"), list):
            raise ValueError("history payload missing 'history' list")

    try:
        payload = await importer._fetch_json(  # noqa: SLF001 - same package
            HISTORY_PATH_FMT.format(entry_id=entry_id), validator=_validate
        )
        rows = [r for r in payload.get("history") or [] if isinstance(r, dict)]
        return rows
    except Exception as exc:  # noqa: BLE001 â€” history is optional signal
        logger.info("fpl_truth: history unavailable for %s: %s", entry_id, exc)
        return []


async def _fetch_confirmed_transfers(
    importer: FplSquadImporter, entry_id: int
) -> list[dict[str, Any]]:
    """Best-effort fetch of the live transfers endpoint; empty on failure."""
    try:
        payload: Any = await importer._fetch_json(  # noqa: SLF001 - same package
            TRANSFERS_PATH_FMT.format(entry_id=entry_id),
            validator=lambda d: (
                None
                if isinstance(d, list)
                else (_ for _ in ()).throw(ValueError("transfers payload not a list"))
            ),
        )
        if not isinstance(payload, list):
            return []
        return [t for t in payload if isinstance(t, dict)]
    except Exception as exc:  # noqa: BLE001 — transfers is optional signal
        logger.info("fpl_truth: transfers endpoint unavailable for %s: %s", entry_id, exc)
        return []


def classify_picks_error(exc: BaseException, path_hint: str) -> int:
    """Map an importer exception to an HTTP-ish status for picks payloads.

    Only genuine 404s (typed ``FplPicksNotSaved`` or an attempts string that
    literally contains "404"/"Not found") count as *not saved*. Everything
    else is reported as 500 instead of being silently downgraded â€” this is
    the fix for the old string-match bug where ANY error mentioning "picks"
    was treated as 404 and masked real outages.
    """
    from fpl_intelligence.squad.fpl_import import FplPicksNotSaved  # noqa: PLC0415

    if isinstance(exc, FplPicksNotSaved):
        return 404
    text = f"{type(exc).__name__}: {exc}"
    if "404" in text or "Not found" in text:
        return 404
    _ = path_hint
    return 500


async def fetch_fpl_truth(entry_id: int | str, importer: FplSquadImporter) -> FplTruth:
    """Fetch every truth signal for one entry through the egress chain.

    Raises whatever ``importer._fetch_json`` raises for hard failures of the
    entry endpoint itself (typed ``FplEntryNotFound`` / rate-limit errors);
    optional signals (history, transfers) degrade to empty without failing.
    """
    eid = int(entry_id)

    entry, bootstrap = await asyncio.gather(
        importer._fetch_json(  # noqa: SLF001 - same package
            f"/api/entry/{eid}/",
            validator=lambda d: (
                None
                if isinstance(d, dict) and "id" in d
                else (_ for _ in ()).throw(ValueError("entry payload missing 'id'"))
            ),
        ),
        importer._fetch_json(  # noqa: SLF001 - same package
            "/api/bootstrap-static/",
            validator=lambda d: (
                None
                if isinstance(d, dict) and "elements" in d
                else (_ for _ in ()).throw(ValueError("bootstrap payload missing 'elements'"))
            ),
        ),
    )

    current_event = int(entry.get("current_event") or 1)
    entry_name = entry.get("name")

    next_gw: int | None = None
    try:
        from fpl_intelligence.sync.gameweek_clock import pick_target_event  # noqa: PLC0415

        events = bootstrap.get("events") if isinstance(bootstrap, dict) else None
        if isinstance(events, list) and events:
            next_gw = pick_target_event(events)
    except Exception as exc:  # noqa: BLE001 â€” never fail truth on clock
        logger.debug("fpl_truth: next_gw resolve failed: %s", exc)
    if next_gw is None:
        next_gw = current_event + 1
    next_gw = int(next_gw)

    async def _picks(gw: int) -> tuple[list[int], int]:
        try:
            payload = await importer._fetch_json(  # noqa: SLF001 - same package
                f"/api/entry/{eid}/event/{gw}/picks/",
                validator=lambda d: (
                    None
                    if isinstance(d, dict) and "picks" in d
                    else (_ for _ in ()).throw(ValueError("picks payload missing 'picks'"))
                ),
            )
            ids = [
                int(p["element"])
                for p in payload.get("picks") or []
                if isinstance(p, dict) and "element" in p
            ]
            return sorted(ids), 200
        except Exception as exc:  # noqa: BLE001 â€” classified below
            return [], classify_picks_error(exc, f"/event/{gw}/picks/")

    (
        cur_ids,
        cur_status,
    ), (nxt_ids, nxt_status), history_rows, live_transfers = await asyncio.gather(
        _picks(current_event),
        _picks(next_gw),
        _fetch_history(importer, eid),
        _fetch_confirmed_transfers(importer, eid),
    )

    history_row = next((r for r in history_rows if int(r.get("event") or 0) == next_gw), None)
    latest_row = max(history_rows, key=lambda r: int(r.get("event") or 0)) if history_rows else None

    pending: list[dict[str, Any]] = []
    for tr in live_transfers:
        try:
            ev = int(tr.get("event"))
        except (TypeError, ValueError):
            continue
        if ev != next_gw:
            continue
        try:
            pending.append(
                {
                    "element_in": int(tr.get("element_in")),
                    "element_out": int(tr.get("element_out")),
                    "cost": int(tr.get("event_cost") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    # Also honour element_in/out already written into history blocks for the
    # target GW (the second half of the mid-window quirk). Block rows inherit
    # the block's event; they carry no event field of their own.
    for block in history_rows:
        try:
            if int(block.get("event")) != next_gw:
                continue
        except (TypeError, ValueError):
            continue
        for tr in block.get("transfers") or []:
            if not isinstance(tr, dict):
                continue
            try:
                raw_in = tr.get("element_in")
                raw_out = tr.get("element_out")
                if raw_in is None or raw_out is None:
                    continue
                pending.append(
                    {
                        "element_in": int(raw_in),
                        "element_out": int(raw_out),
                        "cost": int(tr.get("event_cost") or 0),
                    }
                )
            except (TypeError, ValueError):
                continue

    return FplTruth(
        current_event=current_event,
        next_gw=next_gw,
        picks_current_ids=cur_ids,
        picks_current_status=cur_status,
        picks_next_ids=nxt_ids,
        picks_next_status=nxt_status,
        history_row=history_row,
        latest_history_row=latest_row,
        next_gw_transfers=pending,
        entry_name=entry_name,
    )


def history_note(truth: FplTruth) -> str:
    """The sentence shown in the fpl-view card ("FPL history: ...")."""
    target = truth.next_gw or truth.current_event
    row = truth.history_row
    if row is not None:
        raw = row.get("event_transfers")
        n = int(raw) if isinstance(raw, (int, str)) else None
        if n is not None:
            plural = "" if n == 1 else "s"
            return f"FPL history: {n} transfer{plural} made for GW{target}"
    latest = truth.latest_history_row
    if latest is not None:
        try:
            raw_le = latest.get("event")
            raw_lt = latest.get("event_transfers")
            if not isinstance(raw_le, (int, str)):
                raise ValueError("event missing")
            le = int(raw_le)
            lt = int(raw_lt) if isinstance(raw_lt, (int, str)) else 0
        except (TypeError, ValueError):
            return "FPL history: no GW{} row yet — GW not finished".format(target)
        plural = "" if lt == 1 else "s"
        return (
            f"FPL history: no GW{target} row yet — GW not finished · "
            f"latest GW{le}: {lt} transfer{plural}"
        )
    return "FPL history: no GW{} row yet — GW not finished".format(target)


def rebuild_squad_ids_from_swaps(
    saved_ids: list[int], swaps: list[dict[str, Any]]
) -> tuple[list[int], list[int], list[int]] | None:
    """Apply element_in/out swaps to a saved squad id list (pure).

    Returns ``(new_ids, ins_applied, outs_removed)`` or ``None`` when the
    swap list is unusable (missing sides, players not in squad, wrong size).
    """
    ids = [int(p) for p in saved_ids]
    ins: list[int] = []
    outs: list[int] = []
    for sw in swaps:
        try:
            eid_in = int(sw["element_in"])
            eid_out = int(sw["element_out"])
        except (KeyError, TypeError, ValueError):
            return None
        if eid_out not in ids:
            return None
        if eid_in in ids:
            continue
        ids.remove(eid_out)
        ids.append(eid_in)
        ins.append(eid_in)
        outs.append(eid_out)
    if not ins:
        return None
    return sorted(ids), ins, outs
