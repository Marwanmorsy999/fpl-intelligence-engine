"""Offline signal-lift evaluation for PIT availability flags."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fpl_intelligence.availability.historical.materialize_pit import MaterializeReport

_RESTRICTED = {"out", "suspended", "doubtful", "questionable", "suspect"}
_HARD_OUT = {"out", "suspended"}


@dataclass
class SignalLiftReport:
    db_linked: bool = False
    matched_rows: int = 0
    control_rows: int = 0
    restricted_rows: int = 0
    available_rows: int = 0
    restricted_mean_minutes: float | None = None
    available_mean_minutes: float | None = None
    hard_out_mean_minutes: float | None = None
    restricted_start_rate: float | None = None
    available_start_rate: float | None = None
    start_rate_delta: float | None = None
    minutes_delta: float | None = None
    by_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        hard_out_n = sum(self.by_status.get(s, {}).get("n", 0) for s in _HARD_OUT)
        signal_ok = (
            self.minutes_delta is not None and self.minutes_delta > 0
        ) or (
            self.hard_out_mean_minutes is not None
            and self.hard_out_mean_minutes <= 5.0
            and hard_out_n >= 10
        )
        return {
            "db_linked": self.db_linked,
            "matched_rows": self.matched_rows,
            "control_rows": self.control_rows,
            "restricted_rows": self.restricted_rows,
            "available_rows": self.available_rows,
            "restricted_mean_minutes": self.restricted_mean_minutes,
            "available_mean_minutes": self.available_mean_minutes,
            "hard_out_mean_minutes": self.hard_out_mean_minutes,
            "restricted_start_rate": self.restricted_start_rate,
            "available_start_rate": self.available_start_rate,
            "minutes_delta": self.minutes_delta,
            "start_rate_delta": self.start_rate_delta,
            "by_status": self.by_status,
            "signal_direction_ok": signal_ok,
            "notes": self.notes,
        }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _start_rate(values: list[float]) -> float | None:
    return round(sum(v >= 60 for v in values) / len(values), 6) if values else None


def evaluate_signal_lift(report: MaterializeReport, db: Any | None = None) -> SignalLiftReport:
    """Measure flagged PIT players against unflagged players in the same gameweeks."""
    out = SignalLiftReport()
    if db is None:
        counts: dict[str, int] = {}
        for snapshot in report.snapshots:
            for event in snapshot.events:
                status = str(event.get("status") or "unknown").lower()
                counts[status] = counts.get(status, 0) + 1
        out.matched_rows = sum(counts.values())
        out.by_status = {status: {"events": n} for status, n in sorted(counts.items())}
        out.notes.append("No database session; structural counts only.")
        return out

    from sqlalchemy import select
    from fpl_intelligence.db.models import Gameweek, PlayerExternalId, PlayerGameweekPerformance
    from fpl_intelligence.availability.historical.deadlines import resolve_season

    ext_rows = db.execute(
        select(PlayerExternalId.provider_player_id, PlayerExternalId.player_id).where(
            PlayerExternalId.provider.in_(["real_fpl", "real_fpl_bootstrap", "official_fpl"])
        )
    ).all()
    canonical = {str(provider_id): int(player_id) for provider_id, player_id in ext_rows}

    restricted: list[float] = []
    available: list[float] = []
    hard_out: list[float] = []
    control: list[float] = []
    buckets: dict[str, list[float]] = {}
    seen_controls: set[tuple[int, int]] = set()

    for snapshot in report.snapshots:
        gw_num = snapshot.cutoff.gameweek
        if gw_num is None:
            continue
        season = resolve_season(db, snapshot.cutoff.season_code)
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

        flagged_ids: set[int] = set()
        for event in snapshot.events:
            player_id = canonical.get(str(event.get("player_id") or ""))
            if player_id is None:
                continue
            flagged_ids.add(player_id)
            perf = db.scalar(
                select(PlayerGameweekPerformance).where(
                    PlayerGameweekPerformance.player_id == player_id,
                    PlayerGameweekPerformance.gameweek_id == gw.id,
                )
            )
            if perf is None:
                continue
            minutes = float(perf.minutes or 0)
            status = str(event.get("status") or "unknown").lower()
            buckets.setdefault(status, []).append(minutes)
            out.matched_rows += 1
            if status in _RESTRICTED:
                restricted.append(minutes)
            if status in _HARD_OUT:
                hard_out.append(minutes)

        # The provider omits default-available rows, so the control group is the
        # unflagged player population for the exact same season/gameweek.
        perfs = db.scalars(
            select(PlayerGameweekPerformance).where(
                PlayerGameweekPerformance.season_id == season.id,
                PlayerGameweekPerformance.gameweek_id == gw.id,
            )
        ).all()
        for perf in perfs:
            key = (int(perf.player_id), int(gw.id))
            if perf.player_id in flagged_ids or key in seen_controls:
                continue
            seen_controls.add(key)
            control.append(float(perf.minutes or 0))

    out.db_linked = True
    out.restricted_rows = len(restricted)
    out.control_rows = len(control)
    out.available_rows = len(control)
    out.restricted_mean_minutes = _mean(restricted)
    out.available_mean_minutes = _mean(control)
    out.hard_out_mean_minutes = _mean(hard_out)
    out.restricted_start_rate = _start_rate(restricted)
    out.available_start_rate = _start_rate(control)
    if out.restricted_mean_minutes is not None and out.available_mean_minutes is not None:
        out.minutes_delta = round(out.available_mean_minutes - out.restricted_mean_minutes, 4)
    if out.restricted_start_rate is not None and out.available_start_rate is not None:
        out.start_rate_delta = round(out.available_start_rate - out.restricted_start_rate, 6)
    out.by_status = {
        status: {"n": len(values), "mean_minutes": _mean(values), "start_rate": _start_rate(values)}
        for status, values in sorted(buckets.items())
    }
    if out.matched_rows == 0:
        out.notes.append("No flagged player-gameweek performance rows matched; lift is unmeasured.")
    elif out.control_rows == 0:
        out.notes.append("No unflagged control player-gameweeks matched; comparative lift is unmeasured.")
    elif out.to_dict()["signal_direction_ok"]:
        out.notes.append("Restricted availability statuses show the expected suppression signal against an unflagged same-gameweek control group.")
    else:
        out.notes.append("Signal direction is inconclusive on this sample.")
    return out
