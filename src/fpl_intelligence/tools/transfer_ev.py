"""Phase 2.2 — transfer Expected-Value calculator.

EV of swapping ``player_out`` for ``player_in`` over a horizon:

    xpts_gain = Σ future mean-xPTS(in) - Σ future mean-xPTS(out)
    risk      = price_volatility(in) * |price_in - price_out|
    ev        = xpts_gain - risk

``price_volatility`` models how much of the gain a price swing could eat;
callers pass a per-player map (e.g. derived from recent transfer velocity).
Missing volatility defaults to :data:`DEFAULT_PRICE_VOLATILITY`.

Confidence percent: treating each player's weekly CI spread as a Gaussian
(``sd = (upper - mean) / 1.96``, falling back to :data:`DEFAULT_SD`), the
chance the swap beats zero is ``Φ(gain / sqrt(sd_in² + sd_out²))`` × 100.

Prices are **£m floats** here (convert FPL tenth-units at the call site).
Missing predictions for either player ⇒ the pair is unscorable; callers get
``None`` / exclusion rather than a crash (Safety rule).

:func:`get_top_transfers` walks every out-player × affordable in-player
combo (~15 × ~100 — no heuristic pruning needed), never proposes buying a
player already owned, honours ``bank``, and returns the top-N by EV.
"""

from __future__ import annotations

from collections.abc import Iterable
from statistics import NormalDist
from typing import Any

#: £m of EV written off per unit of (volatility × price gap) when the caller
#: supplies no volatility signal for the incoming player.
DEFAULT_PRICE_VOLATILITY = 0.10

#: Weekly SD assumed when an ensemble entry carries no usable interval.
DEFAULT_SD = 2.5

#: Two-sided 95% z-score constant (mirrors ensemble_xpts.Z_95).
Z_95 = 1.96


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _player_name(player: Any) -> str:
    name = _field(player, "name") or _field(player, "web_name")
    pid = _field(player, "id")
    return str(name) if name else f"Player {pid}"


def estimate_entry_sd(entry: Any) -> float:
    """Weekly SD implied by one prediction entry's 95% interval."""
    try:
        upper = float(_field(entry, "upper"))
        mean = float(_field(entry, "mean"))
    except (TypeError, ValueError):
        return DEFAULT_SD
    if upper <= mean:
        return DEFAULT_SD
    sd = (upper - mean) / Z_95
    return sd if sd > 0 else DEFAULT_SD


def sum_future_xpts(
    predictions: dict[Any, dict[Any, Any]], player_id: int, remaining_gws: Iterable[int]
) -> float | None:
    """Σ mean-xPTS over the horizon; ``None`` when the player is unknown."""
    per_gw = predictions.get(int(player_id))
    if not per_gw:
        return None
    total = 0.0
    seen = False
    for gw in remaining_gws:
        entry = per_gw.get(int(gw))
        if not entry:
            continue
        mean = _field(entry, "mean")
        if mean is None:
            continue
        total += float(mean)
        seen = True
    return total if seen else None


def calculate_confidence(
    player_in: Any,
    player_out: Any,
    remaining_gws: Iterable[int],
    predictions: dict[Any, dict[Any, Any]],
) -> float:
    """Probability (%) that the swap's true point gain is positive."""
    in_id = int(_field(player_in, "id"))
    out_id = int(_field(player_out, "id"))
    gws = list(remaining_gws)

    mean_in = sum_future_xpts(predictions, in_id, gws)
    mean_out = sum_future_xpts(predictions, out_id, gws)
    if mean_in is None or mean_out is None:
        # Unprovable either way — neutral 50 rather than fake certainty.
        return 50.0

    first_gw = int(gws[0]) if gws else None
    sd_in = estimate_entry_sd(predictions.get(in_id, {}).get(first_gw))
    sd_out = estimate_entry_sd(predictions.get(out_id, {}).get(first_gw))
    sigma = (sd_in**2 + sd_out**2) ** 0.5
    gain = mean_in - mean_out
    if sigma <= 0:
        return 100.0 if gain > 0 else (0.0 if gain < 0 else 50.0)
    probability_positive = 1.0 - NormalDist(mu=0.0, sigma=sigma).cdf(-gain)
    return round(max(0.0, min(1.0, probability_positive)) * 100.0, 1)


# --------------------------------------------------------------------------- #
# Price volatility + expected value
# --------------------------------------------------------------------------- #


def get_price_volatility(
    player_id: int,
    volatility_map: dict[int, float] | None = None,
) -> float:
    """Per-player £m write-down coefficient (recent transfer activity proxy).

    Falls back to :data:`DEFAULT_PRICE_VOLATILITY` when unmapped.
    """
    if not volatility_map:
        return DEFAULT_PRICE_VOLATILITY
    raw = volatility_map.get(int(player_id))
    if raw is None:
        return DEFAULT_PRICE_VOLATILITY
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PRICE_VOLATILITY
    return value if value >= 0 else 0.0


def calculate_transfer_ev(
    player_out: Any,
    player_in: Any,
    remaining_gws: Iterable[int],
    predictions: dict[Any, dict[Any, Any]],
    *,
    price_volatility: dict[int, float] | None = None,
) -> dict[str, Any] | None:
    """EV of one swap over ``remaining_gws``; ``None`` when unscorable.

    Requires mean-xPTS for BOTH players; price fields default to 0.0 when a
    player row omits them so prediction-only fixtures still rank by gain.
    """
    gws = list(remaining_gws)
    in_id = int(_field(player_in, "id"))
    out_id = int(_field(player_out, "id"))

    xpts_in = sum_future_xpts(predictions, in_id, gws)
    xpts_out = sum_future_xpts(predictions, out_id, gws)
    if xpts_in is None or xpts_out is None:
        return None

    cost_in = _as_float(_field(player_in, "now_cost"))
    cost_out = _as_float(_field(player_out, "now_cost"))
    price_diff = (cost_in or 0.0) - (cost_out or 0.0)

    volatility = get_price_volatility(in_id, price_volatility)
    risk = volatility * abs(price_diff)
    ev = (xpts_in - xpts_out) - risk
    confidence = calculate_confidence(player_in, player_out, gws, predictions)

    return {
        "player_in": _player_name(player_in),
        "player_in_id": in_id,
        "player_out": _player_name(player_out),
        "player_out_id": out_id,
        "xpts_gain": round(xpts_in - xpts_out, 2),
        "price_diff": round(price_diff, 1),
        "risk": round(risk, 2),
        "ev": round(ev, 2),
        "confidence": confidence,
    }


def _as_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _squad_player_ids(squad: Any) -> set[int]:
    """Accept SquadState-like objects, raw lists, or dicts."""
    if squad is None:
        return set()
    if isinstance(squad, (list, tuple, set)):
        raw_ids: Iterable[Any] = squad
    else:
        raw_ids = _field(squad, "player_ids") or []
    ids: set[int] = set()
    for pid in raw_ids:
        try:
            ids.add(int(pid))
        except (TypeError, ValueError):
            continue
    return ids


def get_top_transfers(
    squad: Any,
    bank: float,
    all_players: Iterable[Any],
    predictions: dict[Any, dict[Any, Any]],
    remaining_gws: Iterable[int] | None = None,
    *,
    volatility_map: dict[int, float] | None = None,
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """Best swaps by EV: every squad member × every affordable outsider.

    Affordability honours ``bank`` (£m). Unscorable pairs (missing
    predictions) and same-player no-ops are skipped. Returns up to
    ``top_n`` results sorted by EV descending.
    """
    players = list(all_players)
    owned = _squad_player_ids(squad)
    by_id = {
        int(p["id"]): p for p in (_to_dict(p) for p in players) if p.get("id") is not None
    }
    out_pool = [by_id[pid] for pid in sorted(owned & set(by_id))]

    gws_source: Iterable[int]
    if remaining_gws is not None:
        gws_source = list(remaining_gws)
    elif predictions:
        sample = next(iter(predictions.values()), {})
        gws_source = sorted(
            {int(gw) for gw in sample if str(gw).lstrip("-").isdigit()}
        )
    else:
        gws_source = []

    results: list[dict[str, Any]] = []
    for player_out in out_pool:
        cost_out = _as_float(player_out.get("now_cost")) or 0.0
        for player_in in players:
            in_dict = _to_dict(player_in)
            raw_in_id = in_dict.get("id")
            if raw_in_id is None:
                continue
            try:
                in_id = int(raw_in_id)
            except (TypeError, ValueError):
                continue
            if in_id in owned:
                continue
            cost_in = _as_float(in_dict.get("now_cost")) or 0.0
            if cost_in > cost_out + float(bank or 0.0) + 1e-9:
                continue
            outcome = calculate_transfer_ev(
                player_out,
                player_in,
                gws_source,
                predictions,
                price_volatility=volatility_map,
            )
            if outcome is not None:
                results.append(outcome)

    results.sort(key=lambda r: (-r["ev"], r["player_in"], r["player_out"]))
    return results[: max(top_n, 0)]


def _to_dict(player: Any) -> dict[str, Any]:
    if isinstance(player, dict):
        return dict(player)
    return {
        name: getattr(player, name)
        for name in ("id", "name", "web_name", "now_cost")
        if hasattr(player, name)
    }

