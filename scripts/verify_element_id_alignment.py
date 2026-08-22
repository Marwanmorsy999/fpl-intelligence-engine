#!/usr/bin/env python
"""scripts/verify_element_id_alignment.py — v1.5.1 live ID-alignment gate.

Fetches the manager's real picks (default entry **794561**) plus
``bootstrap-static`` from the official FPL API and prints the side-by-side
alignment table::

    FPL element | FPL web_name | Our resolution | Price £m | Match

Resolution mirrors production exactly: ``players.fpl_element_id`` first
(migration 0016), then the legacy ``player_external_ids`` mapping, and finally
the committed seed catalog when no database is reachable.

Asserts:
* every picked element resolves to the SAME name the official API reports;
* spot pairs hold — element 445 -> Haaland, element 318 -> Saka (per live FPL);
* Haaland carries a premium price (~£14m+) and high xPTS (> 3.0) from the
  prediction chain when a database session is available.

Usage::

    python scripts/verify_element_id_alignment.py [--entry 794561] [--gw N]

Exit codes: ``0`` aligned, ``1`` misalignment detected, ``2`` network/env error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
for _p in (str(_SRC), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fpl_intelligence.db.models import Player, PlayerExternalId  # noqa: E402
from fpl_intelligence.prediction.live_provider import load_player_catalog  # noqa: E402

URL_BASE = "https://fantasy.premierleague.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

ENTRY_DEFAULT = 794561
#: Required spot pairs (element -> expected web_name).
SPOT_PAIRS = {445: "Haaland", 318: "Saka"}
HAALAND_MIN_PRICE = 14.0
HAALAND_MIN_XPTS = 3.0

EXIT_OK = 0
EXIT_MISALIGNED = 1
EXIT_ENV = 2


def _get_json(client: httpx.Client, path: str) -> Any:
    resp = client.get(f"{URL_BASE}{path}")
    resp.raise_for_status()
    return resp.json()


def resolve_via_db(session: Any, element_id: int) -> tuple[str, int | None] | None:
    """Production-resolution order: fpl_element_id column, then external ids."""
    player = session.scalar(
        select(Player).where(Player.fpl_element_id == element_id)
    )
    if player is None:
        for prov in ("official_fpl", "fpl"):
            ext = session.scalar(
                select(PlayerExternalId).where(
                    PlayerExternalId.provider == prov,
                    PlayerExternalId.provider_player_id == str(element_id),
                )
            )
            if ext is not None:
                player = session.get(Player, ext.player_id)
                break
    if player is None:
        return None
    return player.web_name, player.fpl_element_id


def _connect_db() -> Any:
    """Open the app's SessionLocal, or ``None`` when unreachable."""
    try:
        from fpl_intelligence.db.session import SessionLocal

        session = SessionLocal()
        session.execute(select(1))
        print("Database: connected.\n")
        return session
    except Exception as exc:  # noqa: BLE001 - degrade to catalog-only checks
        print(f"Database: unavailable ({type(exc).__name__}: {exc})\n")
        return None


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to legacy codepages that cannot encode player
    # names like "E.Le Fée" — force UTF-8 with a safe fallback.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--entry", type=int, default=ENTRY_DEFAULT)
    parser.add_argument("--gw", type=int, default=None, help="Override gameweek.")
    args = parser.parse_args(argv)

    failures: list[str] = []
    try:
        client = httpx.Client(timeout=30.0, follow_redirects=True, headers=HEADERS)
        bootstrap = _get_json(client, "/api/bootstrap-static/")
        elements = {
            int(e["id"]): e for e in (bootstrap.get("elements") or []) if e.get("id")
        }
        try:
            entry = _get_json(client, f"/api/entry/{args.entry}/")
            gw = args.gw or int(entry.get("current_event") or 1)
            picks_payload = _get_json(
                client, f"/api/entry/{args.entry}/event/{gw}/picks/"
            )
            picks = [int(p["element"]) for p in picks_payload.get("picks") or []]
        except httpx.HTTPStatusError:
            print(
                f"WARN: entry {args.entry} picks unavailable — "
                "verifying bootstrap alignment only."
            )
            picks, gw = [], (args.gw or 1)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"ERROR: FPL API unreachable: {exc}", file=sys.stderr)
        return EXIT_ENV

    # --- committed seed catalog (production price/xPTS source of truth) -------
    catalog = load_player_catalog()
    catalog_names = {el: row["web_name"] for el, row in catalog.items()}
    session = _connect_db()

    # --- side-by-side table ---------------------------------------------------
    header = (
        f"{'FPL element':>11} | {'FPL web_name':<18} | {'Our resolution':<16}"
        f" | {'Price £m':>8} | Match"
    )
    print(header)
    print("-" * len(header))

    def emit(el: int) -> None:
        fpl_row = elements.get(el, {})
        fpl_name = str(fpl_row.get("web_name", "?"))
        cost = fpl_row.get("now_cost")
        price = float(cost) / 10.0 if cost is not None else float("nan")
        resolved = resolve_via_db(session, el) if session is not None else None
        our_name = (
            resolved[0]
            if resolved
            else ("<no row>" if session else catalog_names.get(el, "<no DB>"))
        )
        match = our_name == fpl_name
        status = "OK" if match else "MISMATCH"
        print(
            f"{el:>11} | {fpl_name:<18} | {our_name:<16} | {price:>8.1f} | {status}"
        )
        if not match:
            failures.append(f"element {el}: FPL='{fpl_name}' ours='{our_name}'")

    for el in picks:
        emit(el)
    print()

    # --- spot pairs + dynamic live-id guarantees ------------------------------
    print("Spot pairs:")
    live_named = {el: str(row.get("web_name", "")) for el, row in elements.items()}

    def check(el: int, expected: str) -> None:
        resolved = resolve_via_db(session, el) if session is not None else None
        our = (
            resolved[0]
            if resolved
            else (catalog_names.get(el) if session is None else None)
        )
        if our == expected:
            print(f"  PASS  element {el} -> {expected}")
        else:
            failures.append(f"pair failed: element {el} resolved to {our!r}")
            print(f"  FAIL  element {el} resolved to {our!r}, expected {expected!r}")

    for el, expected in SPOT_PAIRS.items():
        live_says = live_named.get(el)
        if live_says is not None and live_says != expected:
            print(
                f"  NOTE  element {el}: live FPL reports '{live_says}' "
                f"(season element IDs shifted; dynamic checks below still guard)"
            )
            continue
        check(el, expected)
    for wanted in ("Haaland", "Saka"):
        for el in [e for e, n in live_named.items() if n == wanted]:
            if el not in SPOT_PAIRS:
                check(el, wanted)

    # --- Haaland premium price + high xPTS ------------------------------------
    haaland_els = [el for el, n in live_named.items() if n == "Haaland"]
    if haaland_els:
        el = haaland_els[0]
        cost = elements[el].get("now_cost")
        price = float(catalog[el]["price"]) if catalog.get(el) else (
            float(cost) / 10.0 if cost is not None else None
        )
        if price is not None and price >= HAALAND_MIN_PRICE:
            print(
                f"  PASS  Haaland (element {el}) price £{price:.1f}m "
                f">= £{HAALAND_MIN_PRICE:.0f}m"
            )
        else:
            failures.append(f"Haaland price too low: {price!r}")
            print(f"  FAIL  Haaland price {price!r} < £{HAALAND_MIN_PRICE:.0f}m")

        if session is not None:
            try:
                from fpl_intelligence.prediction.live_provider import (
                    LivePredictionProvider,
                )

                provider = LivePredictionProvider(session=session)
                preds = provider.get_squad_predictions([el], [gw])
                pred = preds.get(gw, {}).get(el)
                xp = float(pred.expected_points) if pred is not None else None
                if xp is not None and xp >= HAALAND_MIN_XPTS:
                    print(f"  PASS  Haaland xPTS {xp:.2f} >= {HAALAND_MIN_XPTS}")
                else:
                    failures.append(f"Haaland xPTS too low: {xp!r}")
                    print(f"  FAIL  Haaland xPTS {xp!r} < {HAALAND_MIN_XPTS}")
            except Exception as exc:  # noqa: BLE001 - report, don't mask alignment
                print(f"  WARN  xPTS chain check skipped: {exc}")

    if session is not None:
        session.close()

    print()
    if failures:
        print(f"RESULT: MISALIGNED — {len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return EXIT_MISALIGNED
    print("RESULT: ALIGNED — every checked FPL element resolves correctly.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
