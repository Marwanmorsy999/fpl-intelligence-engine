"""Phase 25 GATE 0 PROOF — executive intelligence, prod-only.

Run against PRODUCTION after the v2.5.0-alpha-engine deploy:

    python scripts/phase25_gate0_proof.py

Produces ``_gate25_proof/gate0_proof.txt`` containing:

1. Transfer ledger rows for entry 2295006 sourced from OFFICIAL history,
   plus the raw history transfers array excerpt straight through prod.
2. Alpha values with BOTH terms (xPTS, pos_avg, ownership) for elements
   411 / 4 / 399 from /api/v1/targets.
3. The horizon planner text for the next gameweek.
4. A check that every "how computed" disclosure line is present.

Every metric that cannot be computed is reported as unavailable — never
invented. Exit code 1 only when a required surface errors out entirely.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx

# Console-safe unicode on Windows codepages.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "https://fpl-intelligence-engine-foundation.vercel.app"
ENTRY = 2295006
ALPHA_IDS = (411, 4, 399)
OUT_DIR = Path("_gate25_proof")


def _get(client: httpx.Client, path: str, params: dict | None = None) -> object:
    resp = client.get(BASE + path, params=params, timeout=90.0)
    print(f"GET {path} {params or ''} -> {resp.status_code}")
    if resp.status_code >= 400:
        print(resp.text[:300])
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

        # --- 1. transfer ledger via the engine (official history server-side) -
        emit("=== 1. TRANSFER LEDGER (/api/v1/transfers/ledger, entry 2295006) ===")
        ledger = _get(client, "/api/v1/transfers/ledger", {"entry_id": ENTRY})
        emit(f"status: {ledger.get('status')}")
        emit(f"source: {ledger.get('source')} · strategy: {ledger.get('strategy')}")
        if ledger.get("note"):
            emit(f"note: {ledger['note']}")
        excerpt = ledger.get("history_excerpt") or []
        if excerpt:
            emit("RAW official history transfers array excerpt:")
            emit(json.dumps(excerpt[0], indent=2))
        else:
            emit(
                "RAW official history excerpt: none — FPL reports zero transfers "
                "recorded for this entry so far this season."
            )
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
            how = rows[0].get("how_computed")
            emit(f"how computed: {how}")
            if "horizon EV" not in str(how):
                failures.append("ledger how_computed line missing formula")
        else:
            emit("transfers: [] — honest empty state (no transfers recorded yet)")
            failures.append("transfer ledger has no rows and no raw excerpt to show")
        emit()

        # --- 2. alpha values + both terms ------------------------------------
        emit("=== 2. ALPHA ENGINE (/api/v1/targets) ===")
        targets = _get(
            client,
            "/api/v1/targets",
            {"session_id": ENTRY, "show_all": "true", "limit": 600},
        )
        emit(f"gameweek: {targets.get('gameweek')}")
        all_targets = targets.get("targets") or []
        emit(f"ranked candidates returned: {len(all_targets)}")
        by_id = {t["player_id"]: t for t in all_targets}
        for pid in ALPHA_IDS:
            t = by_id.get(pid)
            if t is None:
                emit(
                    f"element {pid}: absent from materialized predictions "
                    "(honest unavailable)"
                )
                failures.append(f"element {pid} has no materialized prediction row")
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
        hc = (all_targets or [{}])[0].get("how_computed")
        if hc:
            emit(f"how computed: {hc}")
        else:
            failures.append("targets payload lacks how_computed line")
        focus = targets.get("next_gw_focus") or {}
        emit(f"next-GW focus: GW{focus.get('gameweek')} buys="
             f"{[b.get('web_name') for b in focus.get('buys') or []]}")
        emit(f"position averages shown: {targets.get('position_avgs')}")
        emit()

        # --- 3. planner text ---------------------------------------------------
        emit("=== 3. HORIZON PLANNER (/api/v1/planner) ===")
        plan = _get(client, "/api/v1/planner", {"session_id": ENTRY})
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

    out = OUT_DIR / "gate0_proof.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwritten: {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
