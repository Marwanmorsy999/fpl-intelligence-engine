"""Phase 2.4 — differential finder (low-ownership value detector).

Score blends projected output, the unowned opportunity and price:

    score = (xPTS * (1 - ownership/100)) / (price / 10)

With ``now_cost`` in FPL tenth-units (95 ⇒ £9.5m) this is *expected points
per pound of untapped potential*. Players are bucketed by ownership
(<10% Low · <30% Med · otherwise High) so the UI can badge templates vs
true differentials.

Pure function over injected data: anything without a prediction for the
target gameweek, a usable price, or an ownership figure is skipped — never
guessed (Safety rule). Squad members are excluded via ``exclude_ids``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: Top-N returned by :func:`find_differentials` when not overridden.
DIFFERENTIAL_TOP_N = 10


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


def _player_name(player: Any) -> str:
    name = _field(player, "name") or _field(player, "web_name")
    pid = _field(player, "id")
    return str(name) if name else f"Player {pid}"


def ownership_tier(ownership_pct: float) -> str:
    """Badge bucket by owned-percentage: Low <10, Med <30, else High."""
    if ownership_pct < 10:
        return "Low"
    if ownership_pct < 30:
        return "Med"
    return "High"


def differential_score(xpts: float, ownership_pct: float, now_cost: float) -> float:
    """Spec formula, rounded to 2dp by callers: ``(x*(1-o)) / (p/10)``."""
    return (xpts * (1 - (ownership_pct / 100))) / (now_cost / 10)


def find_differentials(
    all_players: Iterable[Any],
    predictions: dict[Any, dict[Any, Any]],
    gameweek: int,
    *,
    exclude_ids: Iterable[int] | None = None,
    min_xpts: float = 0.0,
    top_n: int = DIFFERENTIAL_TOP_N,
) -> list[dict[str, Any]]:
    """Top-``top_n`` differentials sorted by score descending.

    Requires a ``mean`` xPTS prediction at ``gameweek``. Missing/degenerate
    rows are skipped silently-but-honestly; owned ids never appear.
    """
    excluded = {int(pid) for pid in (exclude_ids or set())}
    results: list[dict[str, Any]] = []
    for player in all_players:
        try:
            pid = int(_field(player, "id"))
        except (TypeError, ValueError):
            continue
        if pid in excluded:
            continue

        entry = predictions.get(pid, {}).get(int(gameweek))
        xpts_raw = _as_float(_field(entry, "mean")) if entry else None
        if xpts_raw is None or xpts_raw <= min_xpts:
            continue

        ownership = _as_float(_field(player, "selected_by_percent"))
        price = _as_float(_field(player, "now_cost"))
        if ownership is None or not price:  # price == 0/None is unusable
            continue

        results.append(
            {
                "player_id": pid,
                "player": _player_name(player),
                "xpts": round(xpts_raw, 2),
                "ownership": round(ownership, 1),
                "price": round(price, 1),
                "score": round(differential_score(xpts_raw, ownership, price), 2),
                "tier": ownership_tier(ownership),
            }
        )

    results.sort(key=lambda r: (-r["score"], r["player_id"]))
    return results[: max(top_n, 0)]
