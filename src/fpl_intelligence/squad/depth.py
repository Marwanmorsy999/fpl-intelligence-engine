"""Phase 22 — decision-depth math (pure, deterministic, unit-testable).

Three read-model builders that sit on top of materialized xPTS + ownership:

* :func:`rank_differentials`   — D1: "xPTS rank minus ownership rank" score;
                                 high scores are players the market underprices.
* :func:`build_watchlist`      — D2: ranked transfer-IN candidates per needed
                                 position even when the verdict is a roll.
* :func:`captain_comparison`   — D3: top-3 armband cards plus the vice EV line
                                 ("if he blanks: next-best + gap").

No I/O happens here: callers pass plain dicts/lists so every rule is
replayable in tests without a database.
"""

from __future__ import annotations

from typing import Any

#: Differential strip size shown on the dashboard.
DIFFERENTIAL_TOP_N = 3

#: Watchlist size per needed position.
WATCHLIST_PER_POSITION = 5


def _rank(items: dict[int, float], *, descending: bool = True) -> dict[int, int]:
    """Dense competition ranking (1 = best); ties share the better rank."""
    ordered = sorted(
        items.items(),
        key=lambda kv: ((-kv[1] if descending else kv[1]), kv[0]),
    )
    ranks: dict[int, int] = {}
    last_value: float | None = None
    last_rank = 0
    for position, (pid, value) in enumerate(ordered, start=1):
        if last_value is not None and value == last_value:
            ranks[pid] = last_rank
        else:
            ranks[pid] = position
            last_rank = position
            last_value = value
    return ranks


def rank_differentials(
    xpts_by_id: dict[int, float],
    ownership_by_id: dict[int, float],
    *,
    exclude_ids: set[int] | None = None,
    min_xpts: float = 2.0,
    top_n: int = DIFFERENTIAL_TOP_N,
) -> list[dict[str, Any]]:
    """Top differentials by ``ownership_rank - xpts_rank`` (higher = better).

    Only players present in BOTH maps qualify; a low-xpts floor keeps the
    strip from recommending non-starters. Excluded ids (e.g. the user's own
    squad) never appear — you cannot buy your own player.
    """
    excluded = {int(pid) for pid in (exclude_ids or set())}
    eligible = [
        pid
        for pid in xpts_by_id
        if pid not in excluded
        and pid in ownership_by_id
        and float(xpts_by_id[pid]) >= min_xpts
    ]
    xpts_rank = _rank({pid: float(xpts_by_id[pid]) for pid in eligible})
    own_rank = _rank({pid: float(ownership_by_id[pid]) for pid in eligible})
    scored = sorted(
        eligible,
        key=lambda pid: (-(own_rank[pid] - xpts_rank[pid]), -xpts_rank[pid], pid),
    )
    return [
        {
            "player_id": pid,
            "xpts": round(float(xpts_by_id[pid]), 2),
            "ownership_pct": round(float(ownership_by_id[pid]), 1),
            "xpts_rank": int(xpts_rank[pid]),
            "ownership_rank": int(own_rank[pid]),
            # positive when far fewer managers own him than his xPTS deserves
            "differential_score": int(own_rank[pid] - xpts_rank[pid]),
        }
        for pid in scored[: max(top_n, 0)]
    ]


def watchlist_score(
    *,
    xpts: float,
    fdr_next3: float | None,
    ownership_pct: float | None,
) -> float:
    """Deterministic watchlist ordering key (documented, no hidden weights).

    ``2 * xPTS`` dominates; an easy three-fixture run adds up to ``+2.4``
    (0.8 per point of FDR below neutral 3.0); low ownership adds a small
    differential bonus capped at ``+1.5``.
    """
    score = 2.0 * float(xpts or 0.0)
    if fdr_next3 is not None:
        score += max(0.0, (3.0 - float(fdr_next3))) * 0.8
    if ownership_pct is not None:
        score += max(0.0, (15.0 - float(ownership_pct))) / 10.0
    return round(score, 3)


def build_watchlist(
    candidates: list[dict[str, Any]],
    *,
    needed_positions: list[int],
    per_position: int = WATCHLIST_PER_POSITION,
) -> dict[str, Any]:
    """Group ranked transfer-IN candidates by needed position.

    ``candidates`` items carry ``player_id, web_name, position, price,
    xpts, fdr_next3, ownership_pct``. Returns ``{"positions": {label: [...]}}``
    preserving the caller's position order; each entry gains a one-line
    reason and the documented composite score.
    """
    labels = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    out: dict[str, Any] = {"positions": {}}
    for pos in needed_positions:
        pool = [c for c in candidates if c.get("position") == pos]
        for candidate in pool:
            candidate["score"] = watchlist_score(
                xpts=float(candidate.get("xpts") or 0.0),
                fdr_next3=candidate.get("fdr_next3"),
                ownership_pct=candidate.get("ownership_pct"),
            )
        pool.sort(key=lambda c: (-c["score"], -float(c.get("xpts") or 0.0), c["player_id"]))
        items: list[dict[str, Any]] = []
        for c in pool[: max(per_position, 0)]:
            fdr = c.get("fdr_next3")
            own = c.get("ownership_pct")
            price = c.get("price")
            parts = [f"xPTS {float(c.get('xpts') or 0.0):.1f}"]
            if fdr is not None:
                parts.append(f"FDR {float(fdr):.1f} next 3")
            if own is not None:
                parts.append(f"{float(own):g}% owned")
            if price is not None:
                parts.append(f"£{float(price):.1f}m")
            items.append(
                {
                    "player_id": c["player_id"],
                    "web_name": c.get("web_name") or f"Player {c['player_id']}",
                    "position": pos,
                    "price": price,
                    "xpts": round(float(c.get("xpts") or 0.0), 2),
                    "fdr_next3": round(float(fdr), 2) if fdr is not None else None,
                    "ownership_pct": round(float(own), 1) if own is not None else None,
                    "score": c["score"],
                    "reason": " · ".join(parts),
                }
            )
        out["positions"][labels.get(int(pos), str(pos))] = items
    return out


def captain_comparison(
    xi_with_data: list[dict[str, Any]],
    captain_id: int | None,
    vice_id: int | None,
    *,
    top_n: int = 3,
) -> dict[str, Any]:
    """D3: top-N armband comparison cards plus the vice EV line.

    ``xi_with_data`` items carry ``player_id, web_name, xpts, ownership_pct,
    next_fixture``. The first card is the engine's captain (when he is among
    them); every card states what happens if he blanks: the next-best option
    and the gap in expected points.
    """
    ordered = [p for p in xi_with_data if p.get("xpts") is not None]
    ordered.sort(key=lambda p: (-float(p["xpts"]), int(p["player_id"])))
    if captain_id is not None:
        cap = next((p for p in ordered if int(p["player_id"]) == int(captain_id)), None)
        if cap is not None:
            ordered.remove(cap)
            ordered.insert(0, cap)
    cards: list[dict[str, Any]] = []
    for player in ordered[: max(top_n, 0)]:
        next_best = next((p for p in ordered if p["player_id"] != player["player_id"]), None)
        blank_note = ""
        gap = None
        if next_best is not None:
            gap = round(float(player["xpts"]) - float(next_best["xpts"]), 2)
            blank_note = (
                f"If he blanks: {next_best['web_name']} "
                f"({float(next_best['xpts']):.1f}) · gap {gap:+.1f}"
            )
        cards.append(
            {
                "player_id": int(player["player_id"]),
                "web_name": player.get("web_name") or f"Player {player['player_id']}",
                "xpts": round(float(player["xpts"]), 2),
                "ownership_pct": player.get("ownership_pct"),
                "next_fixture": player.get("next_fixture"),
                "is_captain": bool(
                    captain_id is not None
                    and int(player["player_id"]) == int(captain_id)
                ),
                "blank_note": blank_note,
                "gap_to_next": gap,
                "_is_vice": bool(vice_id is not None and int(player["player_id"]) == int(vice_id)),
            }
        )

    vice_line: dict[str, Any] | None = None
    captain_card = next((c for c in cards if c["is_captain"]), None)
    vice_player = next(
        (p for p in xi_with_data if vice_id is not None and int(p["player_id"]) == int(vice_id)),
        None,
    )
    if captain_card is not None and vice_player is not None and vice_player.get("xpts") is not None:
        vice_line = {
            "vice_id": int(vice_id),
            "vice_name": vice_player.get("web_name") or f"Player {vice_id}",
            "vice_xpts": round(float(vice_player["xpts"]), 2),
            "captain_name": captain_card["web_name"],
            "line": (
                f"Vice {vice_player.get('web_name')} covers "
                f"{float(vice_player['xpts']):.1f} pts if {captain_card['web_name']} blanks"
            ),
        }
    return {"cards": cards, "vice": vice_line}
