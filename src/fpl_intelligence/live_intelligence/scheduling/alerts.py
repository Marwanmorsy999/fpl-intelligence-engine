"""Phase 9.6 — Alert Generator.

Turns freshly-ingested :class:`~fpl_intelligence.live_intelligence.raw_item_ledger.RawItem`
objects into user-facing alerts. Classification is deliberately local and
heuristic (keyword matching over the title + body), so alert generation makes
**no** network calls and needs no API keys. A future LLM-backed classifier can
slot into the same seam (the :class:`AlertGenerator` already paces successive
passes with the Phase 9.1 :class:`RateLimiter` so a network-backed classifier
inherits the same protection).

Supported alert types:

* :attr:`AlertType.INJURY` — injury / absence news (highest priority).
* :attr:`AlertType.AVAILABILITY_RISK` — fitness doubts and ``chance_of_playing_*``
  risk (e.g. the FPL API connector's availability-risk items).
* :attr:`AlertType.TACTICAL_CHANGE` — formation / role / lineup changes.
* :attr:`AlertType.TRANSFER_NEWS` — signings, loans, departures.
* :attr:`AlertType.GENERAL` — other team news worth surfacing.

One item yields **one** alert: the highest-priority type that matches, which keeps
a single news story from flooding the user. Items that match nothing produce no
alert (they are news, not necessarily actionable news).

Error handling is per-item: a malformed item is recorded on the generation report
and skipped; it never aborts the batch. A :attr:`max_alerts_per_pass` cap stops a
buggy source from spamming the user.

This module is additive: it does not modify the quantitative Phases 1–8 stack, it
makes **no** live API calls inside ``pytest`` (tests feed the generator `RawItem`
objects directly and inject the clock / sleep seams), and it hardcodes no keys.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from fpl_intelligence.live_intelligence.rate_limit import (
    MonotonicClock,
    RateLimiter,
    SleepFn,
)
from fpl_intelligence.live_intelligence.raw_item_ledger import RawItem
from fpl_intelligence.live_intelligence.temporal_ledger import Clock, utc_now


class AlertSeverity(StrEnum):
    """How strongly the user should act on an alert."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AlertType(StrEnum):
    """The supported alert categories (injury news, tactical changes, ...)."""

    INJURY = "injury"
    AVAILABILITY_RISK = "availability_risk"
    TACTICAL_CHANGE = "tactical_change"
    TRANSFER_NEWS = "transfer_news"
    GENERAL = "general"


#: Default severity per alert type.
DEFAULT_SEVERITY: Mapping[AlertType, AlertSeverity] = {
    AlertType.INJURY: AlertSeverity.HIGH,
    AlertType.AVAILABILITY_RISK: AlertSeverity.MEDIUM,
    AlertType.TACTICAL_CHANGE: AlertSeverity.MEDIUM,
    AlertType.TRANSFER_NEWS: AlertSeverity.LOW,
    AlertType.GENERAL: AlertSeverity.LOW,
}

#: Default keyword families. Matching is case-insensitive substring matching
#: over ``title + content_text``.
DEFAULT_KEYWORDS: dict[AlertType, tuple[str, ...]] = {
    AlertType.INJURY: (
        "injury",
        "injured",
        "injuries",
        "hamstring",
        "knee",
        "ankle",
        "ruled out",
        "sidelined",
        "setback",
        "knock",
        "strain",
        "sprain",
        "surgery",
        "out for",
        "miss",
    ),
    AlertType.AVAILABILITY_RISK: (
        "chance_of_playing",
        "availability risk",
        "doubtful",
        "fitness test",
        "late fitness",
        "race to be fit",
        "back in training",
        "fit again",
        "will be assessed",
        "doubt",
    ),
    AlertType.TACTICAL_CHANGE: (
        "tactical",
        "formation",
        "system change",
        "position change",
        "role change",
        "dropped",
        "benched",
        "starting eleven",
        "lineup",
    ),
    AlertType.TRANSFER_NEWS: (
        "transfer",
        "signs",
        "signing",
        "joins",
        "loan move",
        "departure",
        "moves to",
        "completed move",
    ),
    AlertType.GENERAL: (
        "press conference",
        "confirmed",
        "announced",
        "reveals",
        "update",
        "news",
    ),
}

#: Most specific → least specific. The first matching family wins.
DEFAULT_TYPE_PRIORITY: tuple[AlertType, ...] = (
    AlertType.INJURY,
    AlertType.AVAILABILITY_RISK,
    AlertType.TACTICAL_CHANGE,
    AlertType.TRANSFER_NEWS,
    AlertType.GENERAL,
)

#: Keyword catalog used by :func:`classify_alert_type`.
KeywordCatalog = Mapping[AlertType, tuple[str, ...]]


def classify_alert_type(
    raw_item: RawItem,
    *,
    keywords: KeywordCatalog | None = None,
) -> AlertType | None:
    """Return the highest-priority alert type matching ``raw_item``, or ``None``.

    The whole title + content (lowercased) is scanned with naive substring
    matching against each keyword family, in :data:`DEFAULT_TYPE_PRIORITY`
    order. The first family with any hit wins.
    """
    needle = f"{raw_item.title}\n{raw_item.content_text}".lower()
    table = keywords if keywords is not None else DEFAULT_KEYWORDS
    for alert_type in DEFAULT_TYPE_PRIORITY:
        if any(phrase in needle for phrase in table.get(alert_type, ())):
            return alert_type
    return None


@dataclass(frozen=True)
class Alert:
    """One user-facing alert derived from an ingested raw item."""

    alert_type: AlertType
    severity: AlertSeverity
    title: str
    body: str
    source_id: str
    created_at: datetime
    player: str | None = None
    team: str | None = None
    url: str | None = None
    external_id: str | None = None
    raw_item_id: int | None = None
    matched_keywords: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_type": self.alert_type.value,
            "severity": self.severity.value,
            "title": self.title,
            "body": self.body,
            "source_id": self.source_id,
            "created_at": self.created_at.isoformat(),
            "player": self.player,
            "team": self.team,
            "url": self.url,
            "external_id": self.external_id,
            "raw_item_id": self.raw_item_id,
            "matched_keywords": list(self.matched_keywords),
        }


@dataclass
class AlertGenerationReport:
    """What one :meth:`AlertGenerator.generate` pass produced."""

    processed: int = 0
    generated: int = 0
    alerts: list[Alert] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "generated": self.generated,
            "alerts": [a.to_dict() for a in self.alerts],
            "errors": list(self.errors),
        }


class AlertGenerator:
    """Classify raw items into alerts, rate-limited and error-isolated.

    Args:
        clock: Wall clock supplying ``created_at`` (injectable for tests).
        monotonic_clock: Monotonic clock backing the :class:`RateLimiter`.
        sleep: Sleep function used by the rate limiter (injectable for tests).
        min_interval_seconds: Minimum gap between successive :meth:`generate`
            passes (``0`` disables pacing).
        max_alerts_per_pass: Hard ceiling on the number of alerts a single
            :meth:`generate` call may produce (flood protection).
        keywords: Override the keyword families used by the default classifier.
        classifier: Override the classification function entirely
            (``RawItem -> AlertType | None``). Used by tests to force failures.
    """

    def __init__(
        self,
        *,
        clock: Clock = utc_now,
        monotonic_clock: MonotonicClock = time.monotonic,
        sleep: SleepFn = time.sleep,
        min_interval_seconds: float = 0.0,
        max_alerts_per_pass: int = 50,
        keywords: KeywordCatalog | None = None,
        classifier: Callable[[RawItem], AlertType | None] | None = None,
    ) -> None:
        if max_alerts_per_pass < 1:
            raise ValueError("max_alerts_per_pass must be at least 1")
        self._clock = clock
        self._rate = RateLimiter(
            min_interval_seconds, clock=monotonic_clock, sleep=sleep
        )
        self._max_alerts_per_pass = int(max_alerts_per_pass)
        self._keywords = dict(keywords or DEFAULT_KEYWORDS)
        self._classify = classifier or classify_alert_type

    @property
    def rate_limiter(self) -> RateLimiter:
        """Expose pacing so callers / tests can inspect the rate-limit stats."""
        return self._rate

    @property
    def max_alerts_per_pass(self) -> int:
        return self._max_alerts_per_pass

    def generate(
        self,
        items: Iterable[RawItem],
        *,
        limit: int | None = None,
    ) -> AlertGenerationReport:
        """Classify a batch of raw items into alerts.

        Each pass acquires the :class:`RateLimiter` first (so successive batches
        can never be hammered — even once the classifier is LLM-backed). A
        classifier failure on one item is recorded on ``report.errors`` without
        aborting the rest of the batch. ``limit`` (explicit cap) and
        ``max_alerts_per_pass`` (flood protection) both bound the batch.
        """
        self._rate.acquire()
        report = AlertGenerationReport()
        for item in items:
            effective_limit = self._max_alerts_per_pass
            if limit is not None and limit < effective_limit:
                effective_limit = limit
            if report.generated >= effective_limit:
                break
            report.processed += 1
            try:
                alert = self._build_alert(item)
            except Exception as exc:  # noqa: BLE001 isolate per-item failures
                report.errors.append(f"{item.external_id or item.title}: {exc}")
                continue
            if alert is None:
                continue
            report.alerts.append(alert)
            report.generated += 1
        return report

    # -- internals -----------------------------------------------------------

    def _build_alert(self, item: RawItem) -> Alert | None:
        alert_type = self._classify(item)
        if alert_type is None:
            return None
        needle = f"{item.title}\n{item.content_text}".lower()
        matched = tuple(
            phrase for phrase in self._keywords.get(alert_type, ()) if phrase in needle
        )
        body = item.content_text
        if len(body) > 500:
            body = f"{body[:500].rstrip()}…"
        return Alert(
            alert_type=alert_type,
            severity=DEFAULT_SEVERITY[alert_type],
            title=item.title,
            body=body,
            source_id=item.source_id,
            created_at=self._clock(),
            player=None,  # resolved entities live in Phase 9.2 evidence, not here
            team=None,
            url=item.url,
            external_id=item.external_id,
            raw_item_id=item.raw_item_id,
            matched_keywords=matched,
        )