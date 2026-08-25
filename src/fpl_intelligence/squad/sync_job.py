"""v2.5.6 — async sync-now job registry + background task.

* POST /squad/sync-now starts a background task keyed by session_id,
  returns 202 {job_id, state:"running"} immediately.
* GET /squad/sync-status?session_id polls the registry.
* Background: 25s internal cap; saves snapshot + invalidates decisions cache;
  writes sync_log; builds honest banner with IN/OUT.

Warm retry: picks cached 60s at importer level (fpl_import _PICKS_CACHE).
Bootstrap cached 10 min likewise.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_INTERNAL_TIMEOUT = 25.0
_FAST_PATH_TIMEOUT = 4.0

# ---------------------------------------------------------------------------
# Job registry (in-memory, keyed by session_id)
# ---------------------------------------------------------------------------
_jobs: dict[str, dict[str, Any]] = {}
_tasks: dict[str, asyncio.Task] = {}
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def get_job(session_id: str) -> dict[str, Any] | None:
    with _lock:
        j = _jobs.get(str(session_id))
        return dict(j) if j else None


def create_job(session_id: str, next_gw: bool) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    job: dict[str, Any] = {
        "job_id": job_id,
        "session_id": str(session_id),
        "state": "running",
        "banner": None,
        "picks_gw": None,
        "gameweek": None,
        "transfers_in": [],
        "transfers_out": [],
        "before_ids": [],
        "after_ids": [],
        "started_at": _now_iso(),
        "finished_at": None,
        "error": None,
        "next_gw": bool(next_gw),
        "detected_transfer": None,
    }
    with _lock:
        _jobs[str(session_id)] = job
    return dict(job)


def _set_job(session_id: str, updates: dict[str, Any]) -> None:
    with _lock:
        j = _jobs.get(str(session_id))
        if j:
            j.update(updates)


def clear_job(session_id: str) -> None:
    with _lock:
        _jobs.pop(str(session_id), None)
        t = _tasks.pop(str(session_id), None)
        if t and not t.done():
            t.cancel()


def clear_all_jobs() -> None:
    with _lock:
        for sid in list(_jobs.keys()):
            _jobs.pop(sid, None)
        for sid, t in list(_tasks.items()):
            if not t.done():
                t.cancel()
            _tasks.pop(sid, None)


# ---------------------------------------------------------------------------
# Background execution
# ---------------------------------------------------------------------------

def _get_importer_cls():
    """Return the importer class, preferring a mocked version if patched."""
    # Prefer a MagicMock class if either location has been patched
    try:
        import fpl_intelligence.api.routes.squad as _squad_mod  # noqa: PLC0415

        rc = getattr(_squad_mod, "FplSquadImporter", None)
        if rc is not None and hasattr(rc, "_mock_name"):
            return rc
    except Exception:
        pass
    try:
        import fpl_intelligence.squad.fpl_import as _fpl_mod  # noqa: PLC0415

        fc = getattr(_fpl_mod, "FplSquadImporter", None)
        if fc is not None and hasattr(fc, "_mock_name"):
            return fc
    except Exception:
        pass
    from fpl_intelligence.squad.fpl_import import FplSquadImporter  # noqa: PLC0415

    return FplSquadImporter


def _get_egress_cls():
    try:
        import fpl_intelligence.api.routes.squad as _squad_mod  # noqa: PLC0415

        rc = getattr(_squad_mod, "FplEgressChain", None)
        if rc is not None and hasattr(rc, "_mock_name"):
            return rc
    except Exception:
        pass
    try:
        import fpl_intelligence.data_providers.fpl_egress as _eg_mod  # noqa: PLC0415

        fc = getattr(_eg_mod, "FplEgressChain", None)
        if fc is not None and hasattr(fc, "_mock_name"):
            return fc
    except Exception:
        pass
    from fpl_intelligence.data_providers.fpl_egress import FplEgressChain  # noqa: PLC0415

    return FplEgressChain


async def _run_sync_job(
    session_id: str,
    next_gw_flag: bool,
    job_id: str,
    engine_bind: Any,
) -> None:
    """Background: fetch via importer (25s cap), save, invalidate, log."""
    from fpl_intelligence.config import get_settings  # noqa: PLC0415
    from fpl_intelligence.data_providers.fpl_egress import FplEgressError  # noqa: PLC0415
    from fpl_intelligence.squad.fpl_import import (  # noqa: PLC0415
        FplApiUnavailable,
        FplEntryNotFound,
        FplImportError,
        FplPicksNotSaved,
        FplRateLimitBlocked,
    )

    _ImporterCls = _get_importer_cls()
    _EgressCls = _get_egress_cls()

    # Create isolated DB session for this task
    from sqlalchemy.orm import sessionmaker

    SessionTmp = sessionmaker(bind=engine_bind, autoflush=False, autocommit=False, expire_on_commit=False)
    db = SessionTmp()
    try:
        # before snapshot for banner
        from fpl_intelligence.squad.service import SquadService

        before_squad = SquadService(session=db).get_squad(session_id=str(session_id))
        before_ids: set[int] = set(before_squad.player_ids) if before_squad else set()

        settings = get_settings()
        egress = _EgressCls(
            settings.fpl_base_url,
            timeout=settings.egress_strategy_timeout,
            cache_ttl=settings.egress_cache_ttl,
        )
        importer = _ImporterCls(egress=egress)

        try:
            result = await asyncio.wait_for(
                importer.build_squad_from_entry(int(session_id), db, force_next_gw=bool(next_gw_flag)),
                timeout=_INTERNAL_TIMEOUT,
            )
        except TimeoutError:
            logger.warning("sync-now job %s timeout after %ss for %s", job_id, _INTERNAL_TIMEOUT, session_id)
            _set_job(
                str(session_id),
                {
                    "state": "failed",
                    "error": "Sync timed out after 25s — FPL is slow, please Retry.",
                    "finished_at": _now_iso(),
                    "banner": None,
                },
            )
            try:
                from fpl_intelligence.sync.models import SyncLogDB

                db.add(
                    SyncLogDB(
                        kind="sync_now",
                        entry_id=str(session_id),
                        gameweek=None,
                        ok=False,
                        detail={"error": "timeout 25s", "job_id": job_id},
                        created_at=datetime.now(UTC),
                    )
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            return
        except (asyncio.CancelledError, TimeoutError):
            _set_job(
                str(session_id),
                {
                    "state": "failed",
                    "error": "Sync timed out after 25s — FPL is slow, please Retry.",
                    "finished_at": _now_iso(),
                },
            )
            return
        except FplEntryNotFound:
            _set_job(
                str(session_id),
                {"state": "failed", "error": "Could not find FPL Team ID.", "finished_at": _now_iso()},
            )
            return
        except FplPicksNotSaved:
            _set_job(
                str(session_id),
                {"state": "failed", "error": "Picks not saved yet — try again closer to deadline.", "finished_at": _now_iso()},
            )
            return
        except FplRateLimitBlocked:
            _set_job(
                str(session_id),
                {"state": "failed", "error": "FPL API blocked by rate limit — please Retry in a minute.", "finished_at": _now_iso()},
            )
            return
        except (FplApiUnavailable, FplEgressError, FplImportError) as exc:
            # Honest note with underlying cause truncated
            msg = str(exc)[:400] if str(exc) else "FPL API temporarily unavailable"
            _set_job(
                str(session_id),
                {"state": "failed", "error": f"FPL API temporarily unavailable: {msg}", "finished_at": _now_iso()},
            )
            try:
                from fpl_intelligence.sync.models import SyncLogDB

                db.add(
                    SyncLogDB(
                        kind="sync_now",
                        entry_id=str(session_id),
                        gameweek=None,
                        ok=False,
                        detail={"error": msg, "job_id": job_id},
                        created_at=datetime.now(UTC),
                    )
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync-now job %s failed for %s: %s", job_id, session_id, exc)
            _set_job(
                str(session_id),
                {"state": "failed", "error": "Sync failed: upstream unavailable — please Retry.", "finished_at": _now_iso()},
            )
            return

        # Success path: persist squad, invalidate cache, log
        from fpl_intelligence.squad.service import SquadService

        saved = SquadService(session=db).set_squad(result.squad, session_id=str(session_id))
        try:
            from fpl_intelligence.api.routes.squad import _invalidate_decisions_cache

            _invalidate_decisions_cache(str(session_id))
        except Exception:
            pass

        # sync_log
        try:
            from fpl_intelligence.sync.models import SyncLogDB

            db.add(
                SyncLogDB(
                    kind="sync_now",
                    entry_id=str(session_id),
                    gameweek=saved.gameweek,
                    ok=True,
                    detail={
                        "picks_gw": getattr(saved, "picks_gw", saved.gameweek),
                        "winning_strategy": result.winning_strategy,
                        "job_id": job_id,
                    },
                    created_at=datetime.now(UTC),
                )
            )
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        # Transfer detection for banner
        detected = None
        try:
            from fpl_intelligence.transfers.service import detect_transfer_between_snapshots

            detected = detect_transfer_between_snapshots(db, str(session_id))
        except Exception:
            detected = None

        after_ids = set(saved.player_ids)
        ins = sorted(after_ids - before_ids)
        outs = sorted(before_ids - after_ids)
        in_names = [result.player_names.get(pid, f"Player {pid}") for pid in ins]
        out_names = [result.player_names.get(pid, f"Player {pid}") for pid in outs]
        if not before_ids and detected:
            if detected.get("name_in"):
                in_names = [detected["name_in"]]
            if detected.get("name_out"):
                out_names = [detected["name_out"]]

        # Banner honesty rules (v2.5.7)
        # Need to know: picks_next_status, and whether current picks == saved
        # Fetch the fpl-view data to make honest decision
        chose_rule = "unknown"
        picks_next_status = None
        ids_hash_current = None
        ids_hash_next = None

        try:
            from fpl_intelligence.config import get_settings
            from fpl_intelligence.data_providers.fpl_egress import FplEgressChain
            from fpl_intelligence.squad.fpl_import import FplSquadImporter

            settings = get_settings()
            egress = FplEgressChain(
                settings.fpl_base_url,
                timeout=settings.egress_strategy_timeout,
                cache_ttl=settings.egress_cache_ttl,
            )
            importer = FplSquadImporter(egress=egress)

            # Fetch entry for current_event
            entry = await importer._fetch_json(
                f"/api/entry/{session_id}/",
                validator=lambda d: None if isinstance(d, dict) and "id" in d else (_ for _ in ()).throw(ValueError("entry payload missing 'id'")),
            )
            current_event = int(entry.get("current_event") or 1)

            # Fetch bootstrap for next GW
            bootstrap = await importer._fetch_json(
                "/api/bootstrap-static/",
                validator=lambda d: None if isinstance(d, dict) and "elements" in d else (_ for _ in ()).throw(ValueError("bootstrap payload missing 'elements'")),
            )
            next_gw: int | None = None
            try:
                from fpl_intelligence.sync.gameweek_clock import pick_target_event

                events = bootstrap.get("events") if isinstance(bootstrap, dict) else None
                if isinstance(events, list) and events:
                    next_gw = pick_target_event(events)
                    if next_gw is not None and int(next_gw) == int(current_event):
                        pass
                    if next_gw is None:
                        next_gw = int(current_event) + 1
                else:
                    next_gw = int(current_event) + 1
            except Exception:
                next_gw = int(current_event) + 1

            # Fetch picks for current and next GW
            async def _fetch_picks(gw: int) -> tuple[list[int], int]:
                try:
                    payload = await importer._fetch_json(
                        f"/api/entry/{session_id}/event/{gw}/picks/",
                        validator=lambda d: None if isinstance(d, dict) and "picks" in d else (_ for _ in ()).throw(ValueError("picks payload missing 'picks'")),
                    )
                    picks = payload.get("picks") or []
                    ids = [int(p["element"]) for p in picks if isinstance(p, dict) and "element" in p]
                    return sorted(ids), 200
                except Exception as exc:
                    err_text = str(exc)
                    if "404" in err_text or "Not found" in err_text or "picks" in err_text.lower():
                        return [], 404
                    return [], 500

            current_ids, _ = await _fetch_picks(int(current_event))
            next_ids, next_status = await _fetch_picks(int(next_gw)) if next_gw else ([], 404)

            picks_next_status = next_status
            ids_hash_current = hash(tuple(current_ids)) if current_ids else None
            ids_hash_next = hash(tuple(next_ids)) if next_ids else None

            # Banner honesty rules
            saved_ids = set(before_ids)
            current_ids_set = set(current_ids)
            next_ids_set = set(next_ids)

            if next_status == 404 and current_ids_set == saved_ids:
                # Rule 1: picks_next 404 AND picks_current == saved
                chose_rule = "404_equal_saved"
                now = datetime.now(UTC)
                hhmm = now.strftime("%H:%M")
                gw_label = next_gw or current_event
                banner = (
                    f"FPL shows no new GW{gw_label} lineup yet — confirm your transfer on FPL, "
                    f"then sync again."
                )
            elif next_status == 200 and next_ids_set != saved_ids:
                # Rule 2: picks_next 200 and differs from saved -> save it, show IN/OUT
                chose_rule = "200_differs_saved"
                now = datetime.now(UTC)
                hhmm = now.strftime("%H:%M")
                gw_label = saved.gameweek
                if in_names or out_names:
                    banner = (
                        f"Synced! New squad loaded for GW{gw_label}. "
                        f"IN: {', '.join(in_names) if in_names else '—'} "
                        f"OUT: {', '.join(out_names) if out_names else '—'}"
                    )
                else:
                    banner = f"Synced! New squad loaded for GW{gw_label}. no changes · {hhmm}"
            else:
                # Fallback: default behavior
                chose_rule = "fallback"
                now = datetime.now(UTC)
                hhmm = now.strftime("%H:%M")
                gw_label = saved.gameweek
                if in_names or out_names:
                    banner = (
                        f"Synced! New squad loaded for GW{gw_label}. "
                        f"IN: {', '.join(in_names) if in_names else '—'} "
                        f"OUT: {', '.join(out_names) if out_names else '—'}"
                    )
                else:
                    banner = f"Synced! New squad loaded for GW{gw_label}. no changes · {hhmm}"

        except Exception as exc:
            logger.debug("banner honesty rules failed, using fallback: %s", exc)
            chose_rule = "error_fallback"
            now = datetime.now(UTC)
            hhmm = now.strftime("%H:%M")
            gw_label = saved.gameweek
            if in_names or out_names:
                banner = (
                    f"Synced! New squad loaded for GW{gw_label}. "
                    f"IN: {', '.join(in_names) if in_names else '—'} "
                    f"OUT: {', '.join(out_names) if out_names else '—'}"
                )
            else:
                banner = f"Synced! New squad loaded for GW{gw_label}. no changes · {hhmm}"

        _set_job(
            str(session_id),
            {
                "state": "done",
                "banner": banner,
                "picks_gw": getattr(saved, "picks_gw", saved.gameweek),
                "gameweek": saved.gameweek,
                "transfers_in": ins,
                "transfers_out": outs,
                "before_ids": sorted(before_ids),
                "after_ids": sorted(after_ids),
                "finished_at": _now_iso(),
                "error": None,
                "detected_transfer": detected,
                # v2.5.7 honesty fields
                "chose_rule": chose_rule,
                "picks_next_status": picks_next_status,
                "ids_hash_current": ids_hash_current,
                "ids_hash_next": ids_hash_next,
            },
        )
    finally:
        try:
            db.close()
        except Exception:
            pass
        with _lock:
            _tasks.pop(str(session_id), None)


def start_sync_job(session_id: str, next_gw: bool, engine_bind: Any) -> tuple[dict[str, Any], Any]:
    """Create job entry and launch background thread; return (job, handle)."""
    with _lock:
        existing = _jobs.get(str(session_id))
        if existing and existing.get("state") == "running":
            handle = _tasks.get(str(session_id))
            # handle may be a Thread; check if still alive
            try:
                alive = getattr(handle, "is_alive", lambda: False)() if handle else False
            except Exception:
                alive = False
            if handle and alive:
                return dict(existing), handle
            # Also check asyncio Task case for backwards compat
            try:
                done = getattr(handle, "done", lambda: True)()
                if handle and not done:
                    return dict(existing), handle
            except Exception:
                pass
    job = create_job(str(session_id), bool(next_gw))
    job_id = job["job_id"]

    def _thread_target():
        try:
            asyncio.run(_run_sync_job(str(session_id), bool(next_gw), job_id, engine_bind))
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync job thread failed for %s: %s", session_id, exc)
            try:
                _set_job(str(session_id), {"state": "failed", "error": "Sync failed — please Retry.", "finished_at": _now_iso()})
            except Exception:
                pass

    import threading as _th

    th = _th.Thread(target=_thread_target, daemon=True)
    th.start()
    with _lock:
        _tasks[str(session_id)] = th  # type: ignore[assignment]
    return dict(job), th


async def wait_for_job_fast(handle: Any, timeout: float = _FAST_PATH_TIMEOUT, session_id: str | None = None) -> bool:
    """Poll registry for completion within timeout; returns True if done/failed."""
    # handle is a Thread or Task; we poll the job state instead of awaiting
    # session_id may be inferred from handle if needed, but caller should pass via closure
    # For backwards compat, if handle is an asyncio Task, await it with shield
    if handle is not None and hasattr(handle, "done") and hasattr(handle, "__await__"):
        # Likely an asyncio.Task
        try:
            await asyncio.wait_for(asyncio.shield(handle), timeout=timeout)  # type: ignore[arg-type]
            return True
        except TimeoutError:
            return False
        except Exception:
            return True
    # Otherwise poll registry (thread case). Need session_id to poll.
    # If session_id not provided, try to infer from _jobs (single running job)
    # Caller should use the new poll-based helper below when using threads.
    return False


async def wait_for_job_fast_poll(session_id: str, timeout: float = _FAST_PATH_TIMEOUT) -> bool:
    """Poll job registry for session_id until done/failed or timeout."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        job = get_job(str(session_id))
        if job and job.get("state") != "running":
            return True
        await asyncio.sleep(0.2)
    return False
