"""Phase 2.5 — price-change predictor (net-transfer pressure model).

FPL reprices players when net transfer volume crosses a threshold that
scales with price. Simplified bands:

* ``now_cost <= £5.0m``   → 15,000 net transfers
* ``now_cost <= £10.0m``  → 20,000 net transfers
* above                   → 25,000 net transfers

``probability = min(1, |net_transfers| / threshold)`` saturates at a
certain move; urgency buckets it (High >70%, Med >40%, else Low). Only
moves with probability > 30% are surfaced — the market does not care about
noise.

``transfers_in``/``transfers_out`` are the latest per-gameweek deltas
ingested from FPL; ``now_cost`` is in **£m floats** for band selection.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: Net-transfer thresholds per price band (£m → count).
THRESHOLD_BUDGET = 15000
THRESHOLD_MID = 20000
THRESHOLD_PREMIUM = 25000

#: Probability bucket edges and the minimum reported probability.
URGENCY_HIGH = 0.7
URGENCY_MED = 0.4
MIN_PROBABILITY = 0.3


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


def _as_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return int(value)


def transfer_threshold(now_cost_millions: float) -> int:
    """Net-transfer count needed to reprice at this price point."""
    if now_cost_millions <= 5.0:
        return THRESHOLD_BUDGET
    if now_cost_millions <= 10.0:
        return THRESHOLD_MID
    return THRESHOLD_PREMIUM


def urgency_bucket(probability: float) -> str:
    """High >70%, Med >40%, else Low."""
    if probability > URGENCY_HIGH:
        return "High"
    if probability > URGENCY_MED:
        return "Med"
    return "Low"


def predict_price_changes(
    all_players: Iterable[Any], *, limit: int | None = None
) -> list[dict[str, Any]]:
    """Players whose transfer pressure implies an imminent price move.

    Sorted by probability descending. Zero-net players have zero
    probability and vanish below the ``MIN_PROBABILITY`` filter naturally.
    """
    results: list[dict[str, Any]] = []
    for player in all_players:
        price = _as_float(_field(player, "now_cost"))
        transfers_in = _as_int(_field(player, "transfers_in"))
        transfers_out = _as_int(_field(player, "transfers_out"))
        if price is None or transfers_in is None or transfers_out is None:
            continue  # incomplete market data ⇒ no prediction, honestly

        net_transfers = transfers_in - transfers_out
        threshold = transfer_threshold(price)
        probability = min(1.0, abs(net_transfers) / threshold)
        if probability <= MIN_PROBABILITY:
            continue

        direction = "Rise" if net_transfers > 0 else "Fall"
        pid = _field(player, "id")
        name = _field(player, "name") or _field(player, "web_name")
        results.append(
            {
                "player_id": pid,
                "player": str(name) if name else f"Player {pid}",
                "current_price": round(price, 1),
                "direction": direction,
                "probability": round(probability * 100, 1),
                "urgency": urgency_bucket(probability),
                "net_transfers": net_transfers,
                "threshold": threshold,
            }
        )

    results.sort(key=lambda r: (-r["probability"], r["player"]))
    if limit is not None:
        results = results[: max(limit, 0)]
    return results
