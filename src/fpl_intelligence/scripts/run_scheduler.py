"""Phase 9.6 — Production scheduler worker.

Long-running process invoked by the PaaS ``worker`` process type (see the
root ``Procfile``). It drives the Phase 9.6 :class:`Scheduler`: live source
connectors are polled on a configurable cadence and each fetched raw item is
forwarded into the Phase 9.2 ingestion pipeline.

Design rules:
* No quantitative Phases 1–8 code is modified.
* No API keys are hardcoded; the RSS feed URL and run cadence come from the
  environment.
* Live HTTP calls are expected here (this is a production worker, not a unit
  test) but every per-item and per-pass failure is isolated so one bad source
  never aborts the loop.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable

from fpl_intelligence.config import get_settings
from fpl_intelligence.db.session import SessionLocal
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

logger = logging.getLogger(__name__)


def _build_connectors() -> dict[str, SourceConnector]:
    """Construct the connector map from the environment.

    The FPL bootstrap connector is always enabled (no key required). The RSS
    connector is enabled only when ``RSS_FEED_URL`` is set.
    """
    connectors: dict[str, SourceConnector] = {"fpl_api": FPLAPIConnector()}
    rss_url = os.environ.get("RSS_FEED_URL")
    if rss_url:
        connectors["rss"] = RSSConnector(rss_url)
    else:
        logger.info("RSS_FEED_URL not set; RSS connector disabled.")
    return connectors


def _make_ingest_sink() -> Callable[..., None]:
    """Return an ingestion sink that persists a raw item via Phase 9.2."""

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


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)

    connectors = _build_connectors()
    scheduler = Scheduler(
        connectors,
        ingest=_make_ingest_sink(),
        alert_generator=AlertGenerator(),
        min_interval_seconds=float(os.environ.get("SCHEDULER_MIN_INTERVAL", "30")),
    )
    interval = float(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "900"))
    logger.info(
        "Starting scheduler worker (interval=%.0fs, connectors=%s)",
        interval,
        list(connectors),
    )

    stop = {"value": False}

    def _handle(signum: int, _frame) -> None:  # noqa: ANN001 - signal handler sig
        logger.info("Received signal %s; shutting down scheduler.", signum)
        stop["value"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    # Own loop so we can honour graceful shutdown between passes.
    while not stop["value"]:
        try:
            report = scheduler.run()
            logger.info("Scheduler pass complete: %s", report.to_dict())
        except Exception as exc:  # noqa: BLE001 - never abort the worker
            logger.exception("Scheduler pass failed: %s", exc)
        # Sleep until the next interval or until a shutdown signal arrives.
        for _ in range(int(interval)):
            if stop["value"]:
                break
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
