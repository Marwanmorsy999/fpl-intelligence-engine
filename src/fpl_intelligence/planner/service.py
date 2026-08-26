"""Phase 25 Gate 0 (T3) — HORIZON PLANNER service.

Two-gameweek lookahead built on the chip simulator and the T2 Alpha engine:

    GW{n}  : buy <A> out <B>   (EV +x.x)
    GW{n+1}: hold | buy <C>    (EV +x.x)

Every number comes from ``predictions_current`` / the Alpha terms; the plan
ships with an explicit assumptions list (bank, free transfers, chips left)
read from the saved squad. The price-movement heuristic is a labelled
high/low chip computed ONLY from bootstrap net transfers + cost-change
events — never percentages.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Net-transfer threshold above which rise pressure reads "high". Documented
#: constant over the raw official counters (no percentages implied).
RISE_PRESSURE_NET_THRESHOLD = 25000


async def price_pressure(db: Any) -> dict[str, Any]:
    """Rise-pressure chip: high/low/unavailable from official counters only."""
    try:
        from fpl_intelligence.config import get_settings
        from fpl_intelligence.data_providers.fpl_egress import (
            FplEgressChain,
            validate_bootstrap_payload,
        )

        cfg = get_settings()
        chain = FplEgressChain(
            cfg.fpl_base_url,
            timeout=cfg.egress_strategy_timeout,
            cache_ttl=600.0,
        )
        payload = await chain.fetch(
            "/api/bootstrap-static/", validator=validate_bootstrap_payload
        )
        elements = payload.get("elements") or []
        rows: list[dict[str, Any]] = []
        for el in elements:
            if not isinstance(el, dict):
                continue
            try:
                pid = int(el.get("id"))
                t_in = int(el.get("transfers_in_event") or 0)
                t_out = int(el.get("transfers_out_event") or 0)
            except (TypeError, ValueError):
                continue
            cost_change = el.get("cost_change_event")
            try:
                cost_change = int(cost_change or 0)
            except (TypeError, ValueError):
                cost_change = 0
            rows.append(
                {
                    "player_id": pid,
                    "net": t_in - t_out,
                    "cost_change_event": cost_change,
                }
            )
        if rows:
            top = sorted(rows, key=lambda r: -r["net"])[:5]
            pressure = "high" if top and top[0]["net"] >= RISE_PRESSURE_NET_THRESHOLD else "low"
            return {
                "pressure": pressure,
                "inputs": (
                    "net transfers this event (bootstrap transfers_in − "
                    "transfers_out) + cost_change_event; threshold "
                    f"{RISE_PRESSURE_NET_THRESHOLD}"
                ),
                "top_risers": [
                    {**r, "web_name": None} for r in top
                ],
                "source": "bootstrap-static",
            }
    except Exception as exc:  # noqa: BLE001 — honest unavailable below
        logger.info("bootstrap pressure unavailable: %s", type(exc).__name__)

    # Fallback: materialized element_facts carry cost_change_event only.
    try:
        from sqlalchemy import select

        from fpl_intelligence.sync.materialized_models import ElementFactDB

        rows = db.execute(select(ElementFactDB)).scalars().all()
        movers = [r for r in rows if (r.cost_change_event or 0) != 0]
        if movers:
            return {
                "pressure": (
                    "high" if any((r.cost_change_event or 0) > 0 for r in movers) else "low"
                ),
                "inputs": (
                    "materialized element_facts.cost_change_event only "
                    "(bootstrap unreachable — transfer counts unavailable)"
                ),
                "top_risers": [],
                "source": "element_facts",
            }
    except Exception as exc:  # noqa: BLE001 — honest unavailable
        logger.warning("element_facts pressure fallback failed: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()

    return {
        "pressure": "unavailable",
        "inputs": "no bootstrap counters and no materialized price changes",
        "top_risers": [],
        "source": None,
    }


def build_plan_text(payload: dict[str, Any]) -> str:
    """Deterministic .txt rendering of the planner payload (for export)."""
    lines: list[str] = []
    lines.append("FPL INTELLIGENCE — HORIZON PLAN")
    lines.append(f"generated: {payload.get('generated_at', '')}")
    lines.append(f"target gameweek: {payload.get('gameweek', '')}")
    lines.append("")
    lines.append("PLAN")
    for step in payload.get("plan_steps") or []:
        ev = step.get("ev")
        ev_txt = "EV unavailable" if ev is None else f"EV {ev:+.1f}"
        lines.append(
            f"GW{step.get('gameweek')}: {step.get('action')} ({ev_txt})"
        )
    lines.append("")
    lines.append("ASSUMPTIONS")
    for a in payload.get("assumptions") or []:
        lines.append(f"- {a}")
    lines.append("")
    pp = payload.get("price_pressure") or {}
    lines.append(
        f"RISE PRESSURE: {pp.get('pressure', 'unavailable')} "
        f"(inputs: {pp.get('inputs', 'n/a')})"
    )
    lines.append("")
    lines.append("HOW COMPUTED")
    lines.append(str(payload.get("how_computed", "")))
    lines.append("")
    lines.append(
        "Every metric above is computed from stored predictions; missing data "
        "is disclosed rather than estimated."
    )
    return "\n".join(lines)
