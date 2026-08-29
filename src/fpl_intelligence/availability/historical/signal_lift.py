"""Offline signal-lift evaluation for PIT availability flags.

Measures whether pre-deadline availability status correlates with actual
minutes / starts in the target gameweek. This does not promote anything to
the live chain; it only reports measurable association quality.

When DATABASE_URL is unavailable, returns a structural report with
``db_linked=False`` and no fabricated lift numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fpl_intelligence.availability.historical.materialize_pit import MaterializeReport

# Statuses expected to suppress minutes/starts relative to available players.
_RESTRICTED = {"out", "suspended", "doubtful", "questionable", "suspect"}


@dataclass
class SignalLiftReport:
    db_linked: bool = False
    matched_rows: int = 0
    restricted_rows: int = 0
    available_rows: int = 0
    restricted_mean_minutes: float | None = None
    available_mean_minutes: float | None = None
    restricted_start_rate: float | None = None  # minutes >= 60
    available_start_rate: float | None = None
    minutes_delta: float | None = None  # available - restricted (positive = signal works)
    start_rate_delta: float | None = None
    by_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_linked": self.db_linked,
            "matched_rows": self.matched_rows,
            "restricted_rows": self.restricted_rows,
            "available_rows": self.available_rows,
            "restricted_mean_minutes": self.restricted_mean_minutes,
            "available_mean_minutes": self.available_mean_minutes,
            "restricted_start_rate": self.restricted_start_rate,
            "available_start_rate": self.available_start_rate,
            "minutes_delta": self.minutes_delta,
            "start_rate_delta": self.start_rate_delta,
            "by_status": self.by_status,
            "notes": self.notes,
            "signal_direction_ok": (
                self.minutes_delta is not None and self.minutes_delta > 0
            ),
        }


def evaluate_signal_lift(
    report: MaterializeReport,
    db: Any | None = None,
) -> SignalLiftReport:
    """Compare PIT flags to actual PlayerGameweekPerformance when DB is present."""
    out = SignalLiftReport()
    if db is None:
        out.notes.append("No database session; offline structural counts only.")
        # Still count flagged events by status for transparency.
        counts: dict[str, int] = {}
        for snap in report.snapshots:
            for ev in snap.events:
                status = str(ev.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
        out.by_status = {k: {"events": v} for k, v in sorted(counts.items())}
        out.matched_rows = sum(counts.values())
        return out

    from sqlalchemy import select

    from fpl_intelligence.db.models import (
        Gameweek,
        PlayerExternalId,
        PlayerGameweekPerformance,
        Season,
    )

    # Build FPL element-id → canonical player_id map.
    ext_rows = db.execute(
        select(PlayerExternalId.provider_player_id, PlayerExternalId.player_id).where(
            PlayerExternalId.provider.in_(["real_fpl", "official_fpl", "real_fpl_bootstrap"])
        )
    ).all()
    canonical = {str(pid): int(cid) for pid, cid in ext_rows}

    restricted_minutes: list[float] = []
    available_minutes: list[float] = []
    status_buckets: dict[str, list[float]] = {}

    for snap in report.snapshots:
        season_code = snap.cutoff.season_code
        gw_num = snap.cutoff.gameweek
        if gw_num is None:
            continue
        season = db.scalar(select(Season).where(Season.code == season_code))
        if season is None:
            continue
        gw = db.scalar(
            select(Gameweek).where(
                Gameweek.season_id == season.id,
                Gameweek.provider_event_id == int(gw_num),
            )
        )
        if gw is None:
            continue

        for ev in snap.events:
            fpl_id = str(ev.get("player_id") or "")
            player_id = canonical.get(fpl_id)
            if player_id is None:
                continue
            perf = db.scalar(
                select(PlayerGameweekPerformance).where(
                    PlayerGameweekPerformance.player_id == player_id,
                    PlayerGameweekPerformance.gameweek_id == gw.id,
                )
            )
            if perf is None:
                continue
            minutes = float(perf.minutes or 0)
            status = str(ev.get("status") or "unknown").lower()
            status_buckets.setdefault(status, []).append(minutes)
            out.matched_rows += 1
            if status in _RESTRICTED:
                restricted_minutes.append(minutes)
            elif status == "available":
                available_minutes.append(minutes)

    out.db_linked = True
    out.restricted_rows = len(restricted_minutes)
    out.available_rows = len(available_minutes)

    def _mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 4) if xs else None

    def _start_rate(xs: list[float]) -> float | None:
        return round(sum(1 for m in xs if m >= 60) / len(xs), 6) if xs else None

    out.restricted_mean_minutes = _mean(restricted_minutes)
    out.available_mean_minutes = _mean(available_minutes)
    out.restricted_start_rate = _start_rate(restricted_minutes)
    out.available_start_rate = _start_rate(available_minutes)

    if out.available_mean_minutes is not None and out.restricted_mean_minutes is not None:
        out.minutes_delta = round(out.available_mean_minutes - out.restricted_mean_minutes, 4)
    if out.available_start_rate is not None and out.restricted_start_rate is not None:
        out.start_rate_delta = round(out.available_start_rate - out.restricted_start_rate, 6)

    for status, mins in sorted(status_buckets.items()):
        out.by_status[status] = {
            "n": len(mins),
            "mean_minutes": _mean(mins),
            "start_rate": _start_rate(mins),
        }

    if out.matched_rows == 0:
        out.notes.append(
            "No player-gameweek performance rows matched; cannot measure lift yet."
        )
    elif out.minutes_delta is not None and out.minutes_delta <= 0:
        out.notes.append(
            "Restricted statuses did not show lower mean minutes than available; "
            "signal not confirmed on this sample."
        )
    elif out.minutes_delta is not None:
        out.notes.append(
            "Restricted statuses show lower mean minutes than available on this sample."
        )
    return out
