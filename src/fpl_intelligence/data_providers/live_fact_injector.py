"""Phase 11.1 — Live Fact Injector.

Converts structured API facts (:class:`PlayerFact`) into hard
:class:`FactOverride` objects that override baseline quantitative predictions.

Override rules (per the Phase 11.1 spec):

* Official FPL ``chance_of_playing`` == 0  -> ``start_probability = 0.0``
* Official FPL ``chance_of_playing`` == 100 -> ``start_probability = 1.0``
* Official FPL ``news`` contains "suspended" -> ``availability_status = suspended``
* API-Football confirmed starting XI -> ``start_probability = 1.0``,
  ``expected_minutes = 90``
* API-Football confirmed bench -> ``start_probability = 0.0``,
  ``expected_minutes = 0``
* API-Football injured/out -> ``start_probability = 0.0``

The injector **never mutates** any Phase 1–8 model. It emits pure
:class:`FactOverride` data; the decision layer applies them through
:class:`~fpl_intelligence.data_providers.decision_bridge.FactOverrideProvider`,
which wraps the baseline provider and post-processes its prediction objects.

Graceful degradation is built in: :meth:`LiveFactInjector.inject_from_connectors`
calls each connector inside its own try/except, so a failing or disabled source
simply contributes no facts instead of aborting the whole pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fpl_intelligence.data_providers.base import DataConnectorError
from fpl_intelligence.data_providers.facts import (
    FactConfidence,
    FactOverride,
    FactSource,
    PlayerFact,
)

#: Source precedence when two facts target the same FPL player id. Higher wins.
_SOURCE_PRIORITY: dict[FactSource, int] = {
    FactSource.API_FOOTBALL: 3,
    FactSource.FPL_OFFICIAL: 2,
    FactSource.FOOTBALL_DATA_ORG: 1,
    FactSource.UNKNOWN: 0,
}


@dataclass
class LiveFactResult:
    """What the injector produced, plus diagnostics for the dry-run report."""

    overrides: list[FactOverride] = field(default_factory=list)
    by_player: dict[int, FactOverride] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overrides": [o.to_dict() for o in self.overrides],
            "player_count": len(self.by_player),
            "diagnostics": self.diagnostics,
        }


class LiveFactInjector:
    """Translate structured facts into fact overrides."""

    def build_overrides(
        self,
        fpl_facts: list[PlayerFact],
        api_football_facts: list[PlayerFact] | None = None,
        football_data_facts: list[PlayerFact] | None = None,
    ) -> list[FactOverride]:
        """Combine facts from all sources into de-duplicated overrides."""
        by_id: dict[int, FactOverride] = {}

        def add(override: FactOverride) -> None:
            existing = by_id.get(override.player_id)
            if (
                existing is None
                or _SOURCE_PRIORITY[override.source] >= _SOURCE_PRIORITY[existing.source]
            ):
                by_id[override.player_id] = override

        for fact in fpl_facts or []:
            if fact.fpl_player_id is None:
                continue
            override = self._from_fpl(fact)
            if override is not None:
                add(override)
        for fact in api_football_facts or []:
            if fact.fpl_player_id is None:
                continue
            override = self._from_api_football(fact)
            if override is not None:
                add(override)
        for fact in football_data_facts or []:
            if fact.fpl_player_id is None:
                continue
            override = self._from_football_data(fact)
            if override is not None:
                add(override)

        return list(by_id.values())

    # -- per-source rules ----------------------------------------------------

    def _from_fpl(self, fact: PlayerFact) -> FactOverride | None:
        assert fact.fpl_player_id is not None
        parts: list[str] = []
        start: float | None = None
        minutes: float | None = fact.expected_minutes
        status: str | None = None

        chance = fact.chance_of_playing
        if chance == 0:
            start = 0.0
            parts.append("FPL chance_of_playing=0")
        elif chance == 100:
            start = 1.0
            parts.append("FPL chance_of_playing=100")
        elif chance is not None:
            start = chance / 100.0
            parts.append(f"FPL chance_of_playing={chance}")

        news = (fact.news or "").lower()
        if "suspended" in news:
            status = "suspended"
            parts.append("FPL news contains 'suspended'")
        elif (
            fact.status
            and not start
            and fact.status
            in (
                "injured",
                "doubtful",
                "unavailable",
                "out",
            )
        ):
            status = fact.status
            parts.append(f"FPL status={fact.status}")

        if start is None and status is None and minutes is None:
            return None
        return FactOverride(
            player_id=fact.fpl_player_id,
            source=FactSource.FPL_OFFICIAL,
            start_probability=start,
            expected_minutes=minutes,
            availability_status=status,
            reason="; ".join(parts) if parts else "FPL fact",
            confidence=FactConfidence.HIGH,
            fetched_at=fact.fetched_at,
            published_at=fact.published_at,
            available_at=fact.available_at,
            temporal_class=fact.temporal_class,
        )

    def _from_api_football(self, fact: PlayerFact) -> FactOverride | None:
        assert fact.fpl_player_id is not None
        start: float | None = None
        minutes: float | None = None
        status: str | None = None
        reason: str = ""

        if fact.is_starting:
            start = 1.0
            minutes = 90.0
            status = "start"
            reason = "API-Football confirmed in starting XI"
        elif fact.is_bench:
            start = 0.0
            minutes = 0.0
            status = "bench"
            reason = "API-Football confirmed on bench"
        elif fact.is_injured:
            start = 0.0
            status = "out"
            reason = "API-Football injury/out report"

        if start is None and status is None:
            return None
        return FactOverride(
            player_id=fact.fpl_player_id,
            source=FactSource.API_FOOTBALL,
            start_probability=start,
            expected_minutes=minutes,
            availability_status=status,
            reason=reason,
            confidence=FactConfidence.HIGH,
            fetched_at=fact.fetched_at,
        )

    def _from_football_data(self, fact: PlayerFact) -> FactOverride | None:
        # football-data.org has no per-player availability feed (see connector).
        return None

    # -- orchestration ------------------------------------------------------

    def inject_from_connectors(
        self,
        *,
        fpl: Any = None,
        api_football: Any = None,
        football_data: Any = None,
        date: str | None = None,
        season: int | None = None,
        fpl_id_map: Mapping[int, int] | None = None,
    ) -> LiveFactResult:
        """Gather facts from connectors (each isolated) and build overrides."""
        diagnostics: dict[str, Any] = {}
        fpl_facts: list[PlayerFact] = []

        if fpl is not None:
            try:
                fpl_facts = fpl.collect_player_facts()
                diagnostics["fpl_official"] = {
                    "enabled": True,
                    "facts": len(fpl_facts),
                }
            except DataConnectorError as exc:
                diagnostics["fpl_official"] = {
                    "enabled": True,
                    "error": str(exc),
                    "facts": 0,
                }

        af_facts: list[PlayerFact] = []
        if api_football is not None:
            if api_football.is_enabled():
                try:
                    af_facts = api_football.collect_player_facts(
                        date=date, season=season, fpl_id_map=fpl_id_map
                    )
                    diagnostics["api_football"] = {
                        "enabled": True,
                        "facts": len(af_facts),
                    }
                except DataConnectorError as exc:
                    diagnostics["api_football"] = {
                        "enabled": True,
                        "error": str(exc),
                        "facts": 0,
                    }
            else:
                diagnostics["api_football"] = {
                    "enabled": False,
                    "reason": "API_FOOTBALL_KEY not set",
                    "facts": 0,
                }

        fd_facts: list[PlayerFact] = []
        if football_data is not None:
            if football_data.is_enabled():
                diagnostics["football_data_org"] = {"enabled": True, "facts": 0}
            else:
                diagnostics["football_data_org"] = {
                    "enabled": False,
                    "reason": "FOOTBALL_DATA_ORG_KEY not set",
                    "facts": 0,
                }

        overrides = self.build_overrides(fpl_facts, af_facts, fd_facts)
        by_player = {o.player_id: o for o in overrides}
        return LiveFactResult(overrides=overrides, by_player=by_player, diagnostics=diagnostics)
