"""Phase 19.0 — track-record scoring math (pure functions, no DB).

Every function answers one question with a signed point delta and an explicit
verdict so the UI can say "the model was right/wrong by X pts" instead of a
vague percentage.

Conventions
-----------
* ``actual`` maps FPL element_id -> raw (non-doubled) gameweek points.
* Missing actuals for a referenced player make the comparison UNSCOREABLE —
  the functions return ``None`` rather than guessing.
* Captain deltas are doubled: the armband is worth 2x the underlying points.
"""

from __future__ import annotations

from typing import Any

#: Verdict labels shared by scorer and API layer.
RIGHT = "right"
WRONG = "wrong"
NEUTRAL = "neutral"


def _verdict(delta: float) -> str:
    if delta > 0:
        return RIGHT
    if delta < 0:
        return WRONG
    return NEUTRAL


def score_captain(
    captain_element_id: int,
    alternative_ids: list[int],
    actual: dict[int, int],
    *,
    hit_cost: int = 0,
) -> dict[str, Any] | None:
    """Captain vs next-best alternative in the same XI.

    The model is RIGHT when the chosen captain outscored every alternative on
    doubled points. ``alternative_ids`` must be non-empty; otherwise there is
    nothing to compare against and we return None honestly.
    """
    cap_pts = actual.get(captain_element_id)
    alt_points = [(pid, actual.get(pid)) for pid in alternative_ids if pid != captain_element_id]
    scored_alts = [p for _, p in alt_points]
    if cap_pts is None or not alt_points or any(p is None for p in scored_alts):
        return None
    best_alt_id, best_alt_pts = max(alt_points, key=lambda t: t[1] or 0)
    # Ties broken by lower element id keep this deterministic for tests.
    for pid, pts in alt_points:
        if pts == best_alt_pts and pid < best_alt_id:
            best_alt_id = pid
            break
    delta = (cap_pts - (best_alt_pts or 0)) * 2 - hit_cost
    return {
        "captain": captain_element_id,
        "best_alternative": best_alt_id,
        "captain_points": cap_pts * 2,
        "alternative_points": (best_alt_pts or 0) * 2,
        "delta": delta,
        "hit_cost": hit_cost,
        "verdict": _verdict(delta),
    }


def score_transfer(
    transfers_in: list[int],
    transfers_out: list[int],
    actual: dict[int, int],
    *,
    hit_cost: int = 0,
) -> dict[str, Any] | None:
    """Transfer-in vs transfer-out net points (minus any hit cost).

    A swap is RIGHT when the incoming player(s) outscored the outgoing ones by
    more than the hit they cost. Zero-hit free transfers need a strictly
    positive edge.
    """
    ins = [(pid, actual.get(pid)) for pid in transfers_in]
    outs = [(pid, actual.get(pid)) for pid in transfers_out]
    if not ins and not outs:
        return None
    if any(p is None for _, p in ins + outs):
        return None
    gained = sum(p or 0 for _, p in ins)
    lost = sum(p or 0 for _, p in outs)
    delta = gained - lost - hit_cost
    return {
        "transfers_in": transfers_in,
        "transfers_out": transfers_out,
        "gained": gained,
        "lost": lost,
        "delta": delta,
        "hit_cost": hit_cost,
        "verdict": _verdict(delta),
    }


def score_xi(
    recommended_xi: list[int],
    actual_xi: list[int],
    actual: dict[int, int],
) -> dict[str, Any] | None:
    """Recommended XI points vs the user's actually-fielded XI.

    Phase 23 (C2): when both XIs are identical there is still an honest
    verdict — NEUTRAL with a stated reason ("XI matched your fielded XI"),
    never a silent None, so the row can never sit "pending" once its
    gameweek's results are ingested. ``None`` is reserved for genuinely
    unscoreable inputs (empty XIs or missing actuals).
    """
    rec_set, act_set = set(recommended_xi), set(actual_xi)
    if not recommended_xi or not actual_xi:
        return None
    if any(pid not in actual for pid in rec_set | act_set):
        return None
    rec_pts = sum(actual[pid] for pid in rec_set)
    user_pts = sum(actual[pid] for pid in act_set)
    delta = rec_pts - user_pts
    identical = rec_set == act_set
    return {
        "recommended_xi": sorted(rec_set),
        "user_xi": sorted(act_set),
        "recommended_points": rec_pts,
        "user_points": user_pts,
        "delta": 0 if identical else delta,
        "verdict": NEUTRAL if identical else _verdict(delta),
        "reason": (
            "XI matched your fielded XI — nothing to grade"
            if identical
            else ""
        ),
    }


def rolling_hit_rate(scores: list[dict[str, Any]], last_n: int = 5) -> dict[str, Any]:
    """Aggregate verdicts into a rolling hit-rate + last-N slice.

    Neutral scores count as hits (the model was not wrong); unscoreable entries
    are excluded upstream so this only ever sees graded rows.
    """
    graded = [s for s in scores if s.get("verdict") is not None]
    hits = sum(1 for s in graded if s.get("verdict") in (RIGHT, NEUTRAL))
    total_delta = sum(int(s.get("delta") or 0) for s in graded)
    return {
        "graded": len(graded),
        "hits": hits,
        "hit_rate": round(hits / len(graded), 3) if graded else None,
        "net_points": total_delta,
        "last_5": graded[-last_n:][::-1],
    }


def compute_calibration(rows: list[tuple[float, int]]) -> dict[str, Any]:
    """Calibration stats over (predicted, actual) pairs.

    Returns MAE (in points), mean bias (+ = over-predicting), bucketed error
    rates, and sample size. An empty ledger yields counts=0 — never invented
    numbers.
    """
    if not rows:
        return {"count": 0, "mae": None, "bias": None, "buckets": {}}
    errors = [float(p) - float(a) for p, a in rows]
    mae = round(sum(abs(e) for e in errors) / len(errors), 3)
    bias = round(sum(errors) / len(errors), 3)
    buckets: dict[str, dict[str, float]] = {}
    edges = [(0.0, 2.0, "<2"), (2.0, 5.0, "2-5"), (5.0, 10.0, "5-10"), (10.0, 1e9, ">10")]
    for lo, hi, label in edges:
        within = [e for e in errors if lo <= abs(e) < hi]
        buckets[label] = {
            "count": len(within),
            "share": round(len(within) / len(errors), 3) if errors else 0.0,
        }
    return {"count": len(rows), "mae": mae, "bias": bias, "buckets": buckets}
