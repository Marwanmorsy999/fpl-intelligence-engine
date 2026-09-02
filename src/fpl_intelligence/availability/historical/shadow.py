"""Phase 7 — PIT shadow-mode comparison harness.

Phase 7 is **BLOCKED — INSUFFICIENT HISTORICAL AVAILABILITY DATA**.
The infrastructure below exists so the moment a credible pre-deadline
availability source is wired, the engine can:

1. Run the live prediction provider AND a PIT-augmented variant in
   parallel on every decision request.
2. Compute divergence metrics (delta xPTS, delta captain, delta XI,
   delta transfers) between the two without surfacing either to the
   user.
3. Persist divergence observations to the database for offline
   calibration analysis.
4. Never change the live recommendation until the calibration gates
   in :class:`fpl_intelligence.availability.historical.pit_audit` and
   :func:`fpl_intelligence.availability.historical.signal_lift`
   explicitly pass.

This module is the boundary that keeps the activation gate honest.
The live code path always serves ``baseline_only=True``; the
``baseline_plus_pit`` path runs as a silent shadow.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ShadowMode(enum.StrEnum):
    """PIT shadow-mode operating state."""

    DISABLED = "disabled"  # No PIT computations are performed.
    BASELINE_ONLY = "baseline_only"  # PIT shadow infrastructure wired, but only baseline ships.
    SHADOW = "shadow"  # PIT computes in parallel; output is logged, never surfaced.
    EVALUATION = "evaluation"  # PIT computes; output is exposed only behind a debug flag.
    LIVE = "live"  # PIT output may reach users (gated — see :mod:`pit_audit`).


@dataclass
class PITDivergence:
    """Per-player divergence between baseline and PIT-augmented predictions."""

    player_id: int
    gameweek: int
    baseline_xpcts: float
    pit_xpcts: float
    delta_xpcts: float
    baseline_minutes: float
    pit_minutes: float
    delta_minutes: float

    @property
    def material_delta(self) -> bool:
        """True when the divergence is large enough to matter for decisions.

        Threshold: |delta_xpcts| >= 1.0 OR |delta_minutes| >= 15.0.
        These are empirically conservative for FPL xPTS bands.
        """
        return abs(self.delta_xpcts) >= 1.0 or abs(self.delta_minutes) >= 15.0


@dataclass
class PITShadowReport:
    """Outcome of one shadow-mode comparison run."""

    entry_id: str
    gameweek: int
    divergences: list[PITDivergence] = field(default_factory=list)
    baseline_xi: list[int] = field(default_factory=list)
    pit_xi: list[int] = field(default_factory=list)
    baseline_captain: int | None = None
    pit_captain: int | None = None
    ran_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    shadow_mode: ShadowMode = ShadowMode.SHADOW

    @property
    def n_material(self) -> int:
        return sum(1 for d in self.divergences if d.material_delta)

    def xi_changed(self) -> bool:
        return sorted(self.baseline_xi) != sorted(self.pit_xi)

    def captain_changed(self) -> bool:
        return (
            self.baseline_captain is not None
            and self.pit_captain is not None
            and int(self.baseline_captain) != int(self.pit_captain)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "gameweek": self.gameweek,
            "n_divergences": len(self.divergences),
            "n_material": self.n_material,
            "xi_changed": self.xi_changed(),
            "captain_changed": self.captain_changed(),
            "baseline_xi": self.baseline_xi,
            "pit_xi": self.pit_xi,
            "baseline_captain": self.baseline_captain,
            "pit_captain": self.pit_captain,
            "ran_at": self.ran_at.isoformat(),
            "shadow_mode": self.shadow_mode.value,
            "divergences": [asdict(d) for d in self.divergences],
        }


class PITShadowRecorder(Protocol):
    """Minimal protocol for persistence of shadow observations."""

    def record(self, report: PITShadowReport) -> None: ...


class InMemoryShadowRecorder:
    """Process-local recorder. Used by tests + dry-runs."""

    def __init__(self) -> None:
        self.reports: list[PITShadowReport] = []

    def record(self, report: PITShadowReport) -> None:
        self.reports.append(report)


def compare_predictions(
    *,
    entry_id: str,
    gameweek: int,
    baseline_predictions: dict[int, dict[str, Any]],
    pit_predictions: dict[int, dict[str, Any]] | None,
    baseline_xi: list[int],
    pit_xi: list[int],
    baseline_captain: int | None,
    pit_captain: int | None,
    shadow_mode: ShadowMode = ShadowMode.SHADOW,
) -> PITShadowReport:
    """Build a :class:`PITShadowReport` from paired predictions.

    ``pit_predictions`` may be ``None`` when the PIT provider has no
    data for this gameweek (the common case today). The returned
    report is still valid; it just records zero divergences and a
    material XI change is impossible.
    """
    divergences: list[PITDivergence] = []
    if pit_predictions is not None:
        for pid, baseline in baseline_predictions.items():
            pit = pit_predictions.get(pid)
            if pit is None:
                continue
            b_xp = float(baseline.get("expected_points", 0.0))
            p_xp = float(pit.get("expected_points", 0.0))
            b_min = float(baseline.get("expected_minutes", 0.0))
            p_min = float(pit.get("expected_minutes", 0.0))
            divergences.append(
                PITDivergence(
                    player_id=int(pid),
                    gameweek=int(gameweek),
                    baseline_xpcts=b_xp,
                    pit_xpcts=p_xp,
                    delta_xpcts=p_xp - b_xp,
                    baseline_minutes=b_min,
                    pit_minutes=p_min,
                    delta_minutes=p_min - b_min,
                )
            )
    return PITShadowReport(
        entry_id=str(entry_id),
        gameweek=int(gameweek),
        divergences=divergences,
        baseline_xi=list(baseline_xi),
        pit_xi=list(pit_xi),
        baseline_captain=baseline_captain,
        pit_captain=pit_captain,
        shadow_mode=shadow_mode,
    )


def render_report_json(report: PITShadowReport) -> str:
    """Serialize a :class:`PITShadowReport` to JSON."""
    return json.dumps(report.to_dict(), default=str, indent=2)


def gate_can_activate_pit(report: PITShadowReport) -> bool:
    """Conservative activation gate.

    Returns ``True`` only when the shadow report shows the PIT path is
    *stable enough* to surface. The gate is intentionally strict:
    shadow_mode must be EVALUATION or LIVE, the captain must not have
    flipped on the most recent run, and at least 80% of player
    divergences must be sub-material.

    This is the single chokepoint that any future "turn on PIT"
    decision must satisfy. The activation rules are explicit and
    audited; they are not magic numbers hidden in the optimizer.
    """
    if report.shadow_mode not in (ShadowMode.EVALUATION, ShadowMode.LIVE):
        return False
    if report.captain_changed():
        return False
    if not report.divergences:
        # No data — never activate blind.
        return False
    material_ratio = report.n_material / len(report.divergences)
    return material_ratio < 0.20
