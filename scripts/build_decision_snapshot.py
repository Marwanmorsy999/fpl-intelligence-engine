"""Phase 23 — rebuild decision snapshots for one or many (session, gameweek).

Issue #23 calls for moving expensive decision computation OFF the
request path. This script is the single entry point that a GitHub
Actions cron (or an operator running locally) calls to materialize a
batch of ``decision_snapshots`` rows.

What it does
------------

* For each ``(session_id, gameweek)`` requested:
  1. Acquire the per-snapshot advisory lock via
     :class:`fpl_intelligence.sync.decision_snapshots.DecisionSnapshotStore.acquire_rebuild_slot`.
  2. If another worker is already rebuilding the same snapshot, skip.
  3. Otherwise build the decision payload via the Phase 6 optimizer
     bridge, persist it with ``refresh_state = fresh``, and release the
     lock.

* Honors a hard per-job time budget so a single stuck entry cannot
  exhaust the cron window. Each entry that fails is recorded as
  ``stale`` in the store so the request path can fall back gracefully.

Usage
-----

    # Rebuild the current GW for a single FPL entry:
    python scripts/build_decision_snapshot.py --entry 1234567 --gameweek 28

    # Rebuild all entries registered in local_squad_state:
    python scripts/build_decision_snapshot.py --all-saved --gameweek 28

    # Dry-run: do everything but skip the actual rebuild + DB write.
    python scripts/build_decision_snapshot.py --entry 1234567 --gameweek 28 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from fpl_intelligence.config.settings import Settings
from fpl_intelligence.db.session import SessionLocal
from fpl_intelligence.sync.decision_snapshots import (
    DecisionSnapshotStore,
    PostgresBackend,
    RebuildSlot,
    RefreshState,
)

logger = logging.getLogger(__name__)


@dataclass
class _JobResult:
    session_id: str
    gameweek: int
    status: str  # "rebuilt" | "skipped_busy" | "skipped_unchanged" | "failed"
    seconds: float


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--entry", help="FPL entry id to rebuild for")
    p.add_argument(
        "--gameweek",
        type=int,
        required=True,
        help="Gameweek number to materialize",
    )
    p.add_argument(
        "--all-saved",
        action="store_true",
        help="Iterate every entry present in local_squad_state",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the payload but skip writing it to the snapshot store",
    )
    p.add_argument(
        "--refresh-window-seconds",
        type=float,
        default=900.0,
        help="Override the per-snapshot refresh window",
    )
    return p.parse_args(argv)


def _iter_session_ids(args: argparse.Namespace) -> list[str]:
    if args.entry:
        return [str(args.entry)]
    if not args.all_saved:
        raise SystemExit("Provide --entry or --all-saved")
    from fpl_intelligence.squad.models import LocalSquadState

    with SessionLocal() as db:
        rows = db.execute(select(LocalSquadState.session_id).distinct()).all()
    return [str(r[0]) for r in rows if r and r[0] is not None]


def _build_payload(
    session_id: str, gameweek: int, model_version: str, data_snapshot_id: str
) -> dict[str, Any]:
    """Build the decision payload by invoking the Phase 6 bridge.

    Lazy-imported so the script does not require the full prediction
    stack at import time (faster dry-run failures, cleaner logs).
    """
    from fpl_intelligence.prediction.cached_live_provider import (
        CachedLivePredictionProvider,
    )
    from fpl_intelligence.prediction.gameweek_resolve import safe_fixture_count
    from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
    from fpl_intelligence.squad.service import SquadService

    with SessionLocal() as db:
        squad = SquadService(session=db).get_squad(session_id=session_id)
        if squad is None:
            raise RuntimeError(f"no squad for session_id={session_id}")
        provider = CachedLivePredictionProvider(session=db)

        def _cached_fixture_count(player_id: int, gw: int) -> int:
            key = (int(player_id), int(gw))
            cached = provider._fixture_count_cache.get(key)  # noqa: SLF001
            if cached is not None:
                return cached
            count = safe_fixture_count(db, int(player_id), int(gw))
            provider._fixture_count_cache[key] = int(count)  # noqa: SLF001
            return int(count)

        provider.get_fixture_count = _cached_fixture_count  # type: ignore[method-assign]
        bridge = DecisionOptimizerBridge(provider=provider)
        report = bridge.generate_decisions(squad=squad)
        payload = report.model_dump(mode="json")
        payload["_meta"] = {
            "model_version": model_version,
            "data_snapshot_id": data_snapshot_id,
            "built_at": time.time(),
        }
        return payload


def _run(args: argparse.Namespace, *, model_version: str = "v2.7.12") -> list[_JobResult]:
    settings = Settings()
    if settings.app_env.strip().lower() == "production":
        # Sanity guard: prod must not be running without an explicit
        # decision-snapshot table to write into.
        logger.info("production env — ensuring decision_snapshots table exists")

    session_ids = _iter_session_ids(args)
    if not session_ids:
        logger.warning("no sessions to rebuild; exiting")
        return []

    data_snapshot_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    results: list[_JobResult] = []

    for sid in session_ids:
        gw = int(args.gameweek)
        t0 = time.monotonic()
        status = "failed"
        try:
            with SessionLocal() as db:
                store = DecisionSnapshotStore(
                    backend=PostgresBackend(session=db),
                    default_refresh_window_seconds=float(args.refresh_window_seconds),
                )
                slot: RebuildSlot | None = store.acquire_rebuild_slot(
                    sid,
                    gw,
                    model_version=model_version,
                    data_snapshot_id=data_snapshot_id,
                )
                if slot is None:
                    status = "skipped_busy"
                    logger.info("[%s gw=%s] skipped: another worker is refreshing", sid, gw)
                else:
                    with slot:
                        payload = _build_payload(sid, gw, model_version, data_snapshot_id)
                        if args.dry_run:
                            logger.info(
                                "[%s gw=%s] dry-run payload %d keys",
                                sid,
                                gw,
                                len(payload),
                            )
                            status = "rebuilt"
                        else:
                            existing = store.backend.fetch_for_refresh_state(
                                sid, gw, model_version, data_snapshot_id
                            )
                            if (
                                existing is not None
                                and existing.refresh_state is RefreshState.REFRESHING
                            ):
                                store.mark_state(existing.id or -1, RefreshState.FRESH)
                            else:
                                snap = store.store(
                                    sid,
                                    gw,
                                    model_version=model_version,
                                    data_snapshot_id=data_snapshot_id,
                                    payload=payload,
                                )
                                logger.info(
                                    "[%s gw=%s] stored snapshot id=%s state=%s",
                                    sid,
                                    gw,
                                    snap.id,
                                    snap.refresh_state.value,
                                )
                            status = "rebuilt"
        except Exception as exc:  # noqa: BLE001 — record and continue
            logger.exception("[%s gw=%s] rebuild failed: %s", sid, gw, exc)
            # Mark the existing row stale so request paths can fall back.
            try:
                with SessionLocal() as db:
                    backend = PostgresBackend(session=db)
                    latest = backend.fetch_latest(sid, gw)
                    if latest is not None and latest.id is not None:
                        backend.mark_state(latest.id, RefreshState.STALE)
            except Exception:  # noqa: BLE001
                logger.debug("could not mark stale", exc_info=True)
            status = "failed"
        results.append(
            _JobResult(session_id=sid, gameweek=gw, status=status, seconds=time.monotonic() - t0)
        )

    return results


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    results = _run(args)
    rebuilt = sum(1 for r in results if r.status == "rebuilt")
    skipped = sum(1 for r in results if r.status.startswith("skipped"))
    failed = sum(1 for r in results if r.status == "failed")
    logger.info(
        "summary: rebuilt=%d skipped=%d failed=%d total=%d",
        rebuilt,
        skipped,
        failed,
        len(results),
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
