"""Phase 25 GATE 0 PROOF — executive intelligence, prod-only.

Run against PRODUCTION after the v2.5.0-alpha-engine deploy:

    python scripts/phase25_gate0_proof.py

Produces ``_gate25_proof/gate0_proof.txt`` containing:

1. Transfer ledger rows for entry 2295006 sourced from OFFICIAL history,
   plus the raw history transfers array excerpt straight from FPL.
2. Alpha values with BOTH terms (xPTS, pos_avg, ownership) for elements
   411 / 4 / 399 from /api/v1/targets.
3. The horizon planner text for the next gameweek.
4. A check that every "how computed" disclosure line is present.

Every metric that cannot be computed is reported as unavailable — never
invented. Exit code 1 only when a required surface errors out entirely.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

BASE = "https://fpl-intelligence-engine-foundation.n"
ENTRY = 2295006
ALPHA_IDS = (411, 4, 399)
OUT_DIR = Path("_gate25_proof")


def _get(client: httpx.Client, path: str) -> object:
    resp = client.get(BASE + path, timeout=45.0)
    print(f"GET {path} -> {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    lines: list[str] = []
    failures: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        lines.append(line)

    with httpx.Client() as client:
        health = _get(client, "/health")
        emit("=== 0. HEALTH ===")
        emit(json.dumps(health))
        emit()

        # --- 1. raw official history excerpt (straight from FPL) -------------
        emit("=== 1. RAW official history transfers excerpt (entry 2295006) ===")
        try:
            raw = _get(client, "https://fantasy.premierleague.com/api/entry/"
                               f"{ENTRY}/history/")
        except Exception as exc:  # noqa: BLE001 — proof reports honestly
            raw = None
            emit(f"(direct FPL fetch unavailable: {type(exc).__name__}: {exc})")
        if isinstance(raw, dict):
            for block in raw.get("history") or []:
                trs = block.get("transfers") or []
                if not trs:
                    continue
                emit(
                    f'event {block.get("event")}: incoming={block.get("event_transfers")} '
                    f"cost={block.get('event_transfers_cost')} point_hits"
                )
                emit("transfers array excerpt:")
                emit(json.dumps(trs[:3], indent=2))
                break
            else:
                emit("(no GW block in official history carries transfers yet)")
        emit()

        # --- 2. transfer ledger via the engine -------------------------------
        emit("=== 2. TRANSFER LEDGER (/api/v1/transfers/ledger) ===")
        ledger = _get(client, f"/api/v1/transfers/ledger?entry_id={ENTRY}")
        emit(f"status: {ledger.get('status')}")
        emit(f"source: {ledger.get('source')}")
        if ledger.get("note"):
            emit(f"note: {ledger['note']}")
        rows = ledger.get("transfers") or []
        if rows:
            emit(f"rows ({len(rows)}):")
            for t in rows[:10]:
                ev = t.get("horizon_ev")
                ev_txt = "EV unavailable" if ev is None else f"horizon EV {ev:+.1f}"
                emit(
                    f'  GW{t["gameweek"]}: IN {t.get("name_in") or t.get("element_in")}'
                    f' OUT {t.get("name_out") or t.get("element_out")}'
                    f' · hit {t.get("cost", 0)} · {ev_txt}'
                    f' · [{t.get("source")}]'
                )
        else:
            emit("transfers: [] (honest empty state)")
        how = (rows or [{}])[0].get("how_computed")
        if how:
            emit(f"how computed: {how}")
            if "horizon EV" not in how:
                failures.append("ledger how_computed line missing formula")
        else:
            failures.append("transfer ledger has no how_computed line")
        emit()

        # --- 3. alpha values + both terms ------------------------------------
        emit("=== 3. ALPHA ENGINE (/api/v1/targets) ===")
        targets = _get(
            client, f"/api/v1/targets?session_id={ENTRY}&show_all=true&limit=50"
        )
        emit(f"gameweek: {targets.get('gameweek')}")
        by_id = {t["player_id"]: t for t in targets.get("targets") or []}
        for pid in ALPHA_IDS:
            t = by_id.get(pid)
            if t is None:
                emit(f"element {pid}: NOT IN TOP RANKING (no materialized prediction)")
                continue
            own = t.get("own_p")
            own_txt = "unavailable" if own is None else f"{own * 100:.1f}%"
            alpha = t.get("alpha")
            alpha_txt = "unavailable" if alpha is None else f"{alpha:+.2f}"
            emit(
                f"element {pid} [{t['web_name']}]: xPTS={t['xpts']} "
                f"pos_avg={t['pos_avg']} edge={t['edge']:+.2f} "
                f"own={own_txt} ({t['ownership_label']}) => Alpha={alpha_txt}"
            )
        missing_alpha = [
            pid for pid in ALPHA_IDS if pid in by_id and by_id[pid].get("alpha") is None
        ]
        if missing_alpha:
            emit(
                f"honest note: Alpha unavailable for {missing_alpha} "
                "(no league/global ownership source)"
            )
        hc = (targets.get("targets") or [{}])[0].get("how_computed")
        if hc:
            emit(f"how computed: {hc}")
        else:
            failures.append("targets payload lacks how_computed line")
        focus = targets.get("next_gw_focus") or {}
        emit(f"next-GW focus: GW{focus.get('gameweek')} buys="
             f"{[b.get('web_name') for b in focus.get('buys') or []]}")
        emit()

        # --- 4. planner text ---------------------------------------------------
        emit("=== 4. HORIZON PLANNER (/api/v1/planner) ===")
        plan = _get(client, f"/api/v1/planner?session_id={ENTRY}")
        if plan.get("status") != "ok":
            emit(f"planner status: {plan.get('status')} — {plan.get('note')}")
            failures.append("planner returned non-ok without a saved squad on prod")
        else:
            emit(plan.get("plan_text") or "(plan_text missing)")
            pp = plan.get("price_pressure") or {}
            emit(f"rise pressure chip: {pp.get('pressure')} (inputs: {pp.get('inputs')})")
            if "%" in str(pp.get("inputs")):
                failures.append("rise pressure must not use percentages")

        emit()
        emit("=== VERDICT ===")
        if failures:
            emit("FAILURES:")
            for f in failures:
                emit(f"- {f}")
        else:
            emit("GATE 0 PROOF: all surfaces answered; disclosures present.")

    OUT = OUT_DIR / "gate0_proof.txt"
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwritten: {OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
