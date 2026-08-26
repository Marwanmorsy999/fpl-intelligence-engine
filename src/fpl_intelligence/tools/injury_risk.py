"""Phase 2.6 — injury / suspension availability-risk model.

Four weighted factors per the Phase 2 design:

* **Age (30%)**         — ``0.3`` over 30, else ``0.1``.
* **Minutes load (30%)**— games-played proxy ``minutes / 90``; ``0.3`` when
                          above ``0.8`` (near ever-present), else ``0.1``.
* **Injury history (20%)** — ``injuries_last_3_seasons / 3`` (1+ injuries
                          in the window already saturates the raw factor).
* **Congestion (20%)**  — fixtures inside the next 14 days; ``0.3`` when
                          more than two, else ``0.1``.

    risk_pct = Σ(factor × weight) × 100

Levels: High >50%, Medium >25%, else Low. Recommendation is one honest
sentence ("Consider selling" only at High).

Missing inputs default to their *low* factor value and are reported in
``data_missing`` so downstream UIs can annotate uncertainty instead of
fabricating age/injury data the engine does not have (Safety rule).
"""

from __future__ import annotations

from typing import Any

#: Thresholds.
AGE_HIGH_RISK = 30.0
LOAD_GAMES_HIGH_RISK = 0.8
CONGESTION_FIXTURES_14D = 2

#: Factor weights.
WEIGHT_AGE = 0.30
WEIGHT_LOAD = 0.30
WEIGHT_HISTORY = 0.20
WEIGHT_CONGESTION = 0.20


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def calculate_injury_risk(player: Any) -> dict[str, Any]:
    """Availability risk percent + level + recommendation for one player."""
    missing: list[str] = []

    age = _as_float(_field(player, "age"))
    if age is None:
        missing.append("age")
    age_factor = 0.3 if (age is not None and age > AGE_HIGH_RISK) else 0.1

    minutes = _as_float(_field(player, "minutes_played"))
    if minutes is None:
        missing.append("minutes_played")
    minutes_load = (minutes or 0.0) / 90.0
    load_factor = 0.3 if minutes_load > LOAD_GAMES_HIGH_RISK else 0.1

    injuries_raw = _as_float(_field(player, "injuries_last_3_seasons"))
    if injuries_raw is None:
        missing.append("injuries_last_3_seasons")
    injury_history = max(0.0, (injuries_raw or 0.0)) / 3.0

    congestion = _as_float(_field(player, "upcoming_fixtures_in_14_days"))
    if congestion is None:
        missing.append("upcoming_fixtures_in_14_days")
    congestion_factor = (
        0.3
        if (congestion is not None and congestion > CONGESTION_FIXTURES_14D)
        else 0.1
    )

    raw_risk = (
        (age_factor * WEIGHT_AGE)
        + (load_factor * WEIGHT_LOAD)
        + (injury_history * WEIGHT_HISTORY)
        + (congestion_factor * WEIGHT_CONGESTION)
    )
    risk_pct = raw_risk * 100.0

    level = "High" if risk_pct > 50 else "Medium" if risk_pct > 25 else "Low"
    recommendation = "Consider selling" if level == "High" else "Monitor"

    pid = _field(player, "id")
    name = _field(player, "name") or _field(player, "web_name")

    return {
        "player_id": pid,
        "player": str(name) if name else f"Player {pid}",
        "risk_pct": round(risk_pct, 1),
        "level": level,
        "recommendation": recommendation,
        "factors": {
            "age": round(age_factor, 2),
            "load": round(load_factor, 2),
            "injury_history": round(injury_history, 3),
            "congestion": round(congestion_factor, 2),
        },
        "data_missing": missing,
    }
