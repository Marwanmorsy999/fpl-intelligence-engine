"""Phase 9.5 — Connector Scheduler.

Orchestrates the fetching of raw items from multiple
:class:`~fpl_intelligence.live_intelligence.connectors.base.SourceConnector`
instances and forwards the freshly-fetched
:class:`~fpl_intelligence.live_intelligence.raw_item_ledger.RawItem` objects to
an ingestion sink (typically ``ingest_raw_text`` from Phase 9.2).

It supports both **manual triggering** (:meth:`run`) and **scheduled execution**
(:meth:`run_scheduled`) with a configurable interval. Failure is contained: a
connector that raises a :class:`SourceConnectorError` is recorded on its stats
entry and does not abort the rest of the run. Errors thrown by the ingestion
sink for a single item are likewise captured per-connector rather than killing
the batch.

An :class:`IngestionSink` is any callable that accepts a :class:`RawItem` and
returns a report (ignored here). Tests pass a trivial recording sink, so the
end-to-end orchestration — fetch -> sink for every connector, error isolation,
totals — is covered offline with mock connectors.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fpl_intelligence.live_intelligence.connectors.base import (
    SourceConnector,
    SourceConnectorError,
)

#: A sink receives a raw item plus the connector that produced it and the
#: dry-run flag, and returns an (ignored) ingestion report.
type IngestionSink = Callable[..., Any]
type ConnectorMap = Mapping[str, SourceConnector]


@dataclass
class ConnectorRunStats:
    """Per-connector outcome for one run."""

    name: str
    fetched: int = 0
    ingested: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fetched": self.fetched,
            "ingested": self.ingested,
            "errors": list(self.errors),
            "succeeded": self.succeeded,
        }


@dataclass
class SchedulerReport:
    """Aggregate result across the connectors that ran."""

    runs: dict[str, ConnectorRunStats] = field(default_factory=dict)

    def stats_for(self, name: str) -> ConnectorRunStats:
        if name not in self.runs:
            self.runs[name] = ConnectorRunStats(name=name)
        return self.runs[name]

    @property
    def connectors_ran(self) -> int:
        return len(self.runs)

    @property
    def total_fetched(self) -> int:
        return sum(r.fetched for r in self.runs.values())

    @property
    def total_ingested(self) -> int:
        return sum(r.ingested for r in self.runs.values())

    @property
    def total_errors(self) -> int:
        return sum(len(r.errors) for r in self.runs.values())

    @property
    def succeeded(self) -> bool:
        return self.total_errors == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "connectors_ran": self.connectors_ran,
            "total_fetched": self.total_fetched,
            "total_ingested": self.total_ingested,
            "total_errors": self.total_errors,
            "succeeded": self.succeeded,
            "connectors": {k: v.to_dict() for k, v in self.runs.items()},
        }


class ConnectorScheduler:
    """Fetch from a set of connectors and push their items through a sink.

    Args:
        connectors: Map of connector name -> :class:`SourceConnector`.
        sink: Callable ``(raw, *, connector, dry_run) -> report`` that persists
            the item (Phase 9.2 ``ingest_raw_text`` in the CLI, a recorder in
            tests).
        sleep: Sleep function used by :meth:`run_scheduled` only (injectable
            so tests need no wall-clock delay).
    """

    def __init__(
        self,
        connectors: ConnectorMap,
        sink: IngestionSink,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._connectors = dict(connectors)
        self._sink = sink
        self._sleep = sleep

    @property
    def connectors(self) -> Mapping[str, SourceConnector]:
        return dict(self._connectors)

    # -- manual triggering --------------------------------------------------

    def _resolve(self, connector: str | None) -> list[str]:
        if connector is None:
            return list(self._connectors.keys())
        if connector not in self._connectors:
            raise KeyError(f"unknown connector '{connector}'; known: {sorted(self._connectors)}")
        return [connector]

    def run(self, *, connector: str | None = None, dry_run: bool = False) -> SchedulerReport:
        """Run one pass over the requested connector(s) and return a report."""
        report = SchedulerReport()
        for name in self._resolve(connector):
            conn = self._connectors[name]
            stats = report.stats_for(name)
            try:
                items = conn.fetch()
            except SourceConnectorError as exc:
                stats.errors.append(f"fetch failed: {exc}")
                continue
            stats.fetched = len(items)
            for item in items:
                try:
                    self._sink(item, connector=conn, dry_run=dry_run)
                except Exception as exc:  # noqa: BLE001 - isolate per-item failures
                    stats.errors.append(f"{item.external_id or item.title}: {exc}")
                    continue
                stats.ingested += 1
        return report

    # -- scheduled execution ------------------------------------------------

    def run_scheduled(
        self,
        *,
        interval_seconds: float,
        iterations: int,
        connector: str | None = None,
        dry_run: bool = False,
        stop_event: Any | None = None,
    ) -> list[SchedulerReport]:
        """Run :meth:`run` in a loop, returning one report per pass.

        ``stop_event`` (e.g. ``threading.Event``) allows graceful shutdown: the
        loop re-checks it before every pass and before the sleep between passes.
        ``iterations`` bounds the number of passes (0 or negative means run
        until stopped, which tests never enter).
        """
        reports: list[SchedulerReport] = []
        passes = 0
        while True:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            reports.append(self.run(connector=connector, dry_run=dry_run))
            passes += 1
            if iterations and passes >= iterations:
                break
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            self._sleep(max(0.0, float(interval_seconds)))
        return reports
