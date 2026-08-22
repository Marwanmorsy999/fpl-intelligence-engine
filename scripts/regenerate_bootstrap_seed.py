#!/usr/bin/env python
"""scripts/regenerate_bootstrap_seed.py — rebuild data/seed/fpl_bootstrap_seed.json.

One live fetch of the official FPL ``bootstrap-static`` endpoint writes the
committed offline seed that powers prices/names/photos everywhere:

* ``POST /api/v1/admin/seed-from-file`` replays it into PostgreSQL;
* ``load_player_catalog`` reads it as the single source of truth for prices;
* every player row carries its OFFICIAL FPL element ``id`` so squad imports can
  join picks against ``players.fpl_element_id`` (v1.5.1 alignment fix).

FPL blocks datacenter IPs (Vercel), so this must run from a non-blocked machine.

Usage::

    python scripts/regenerate_bootstrap_seed.py [--season-code 2026-27]

Exit codes: ``0`` written ok, ``1`` fetch/write error.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = _REPO_ROOT / "data" / "seed" / "fpl_bootstrap_seed.json"

URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

EXIT_OK = 0
EXIT_FAIL = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--season-code",
        default="2026-27",
        help="Season code stored in the seed meta block.",
    )
    args = parser.parse_args(argv)

    print(f"Fetching {URL} ...")
    try:
        resp = httpx.get(URL, headers=HEADERS, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"ERROR: bootstrap-static fetch failed: {exc}", file=sys.stderr)
        return EXIT_FAIL

    teams = [
        {
            "id": int(t["id"]),
            "name": str(t.get("name", "")),
            "short_name": str(t.get("short_name", "") or t.get("name", "")[:3].upper()),
        }
        for t in sorted(data.get("teams", []), key=lambda x: int(x["id"]))
    ]
    events = [
        {"id": int(ev["id"]), "name": str(ev.get("name", f"Gameweek {int(ev['id'])}"))}
        for ev in sorted(data.get("events", []), key=lambda x: int(x["id"]))
    ]
    players = [
        {
            # Official FPL element ID — the key every squad-import join MUST use.
            "id": int(pl["id"]),
            "first_name": str(pl.get("first_name", "")),
            "second_name": str(pl.get("second_name", "")),
            "web_name": str(pl.get("web_name", "")),
            "position": (
                int(pl["element_type"]) if pl.get("element_type") is not None else None
            ),
            "team": int(pl["team"]) if pl.get("team") is not None else None,
            "now_cost": int(pl["now_cost"]) if pl.get("now_cost") is not None else None,
            # FPL photo code -> Premier-League-CDN URLs.
            "code": int(pl["code"]) if pl.get("code") is not None else None,
        }
        for pl in sorted(data.get("elements", []), key=lambda x: int(x["id"]))
    ]

    seed = {
        "meta": {
            "season_code": args.season_code,
            "generated_at": datetime.now(UTC).isoformat(),
            "fetched_from": URL,
        },
        "teams": teams,
        "events": events,
        "players": players,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(seed, indent=2, sort_keys=True), encoding="utf-8")

    named = {p["id"]: p["web_name"] for p in players}
    spot = {k: named.get(k) for k in ("Haaland", "Saka") if k in set(named.values())}
    print(
        json.dumps(
            {
                "ok": True,
                "teams": len(teams),
                "events": len(events),
                "players": len(players),
                "bytes": OUT.stat().st_size,
                "out": str(OUT),
                "spot_checks": {
                    name: [str(pid) for pid, wn in named.items() if wn == nm]
                    for name, nm in (("Haaland", "Haaland"), ("Saka", "Saka"))
                },
            },
            indent=2,
        )
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())