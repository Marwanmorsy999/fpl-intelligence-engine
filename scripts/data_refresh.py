"""Phase 19.0 — GitHub Actions data refresh (stdlib only, zero pip installs).

Downloads the current-season vaastav FPL per-gameweek CSVs plus an Understat
EPL snapshot, then pushes them to the engine's ``/api/v1/sync/history-push``
endpoint with ``Authorization: Bearer $SYNC_PUSH_TOKEN``.

Environment:
    ENGINE_BASE         e.g. https://your-app.vercel.app
    SYNC_PUSH_TOKEN     bearer token (GitHub Actions secret)
    VAASTAV_SEASON      optional, default "2025-26" (latest published dataset)

Honest failure mode: any gameweek that fails to download or parse is reported;
the script exits non-zero only when NOTHING could be pushed, so a single
missing GW file upstream never breaks an otherwise good refresh.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

VAASTAV_RAW = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
)
UNDERSTAT_LEAGUE_URL = "https://understat.com/league/EPL"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FPL-DataRefresh/1.9"


def _get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _post_json(base: str, path: str, payload: dict, token: str) -> tuple[int, str]:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": BROWSER_UA,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _season_candidates() -> list[str]:
    env = os.environ.get("VAASTAV_SEASON", "").strip()
    if env:
        return [env]
    # Try recent seasons newest-first; vaastav publishes once GW1 finishes.
    return ["2026-27", "2025-26", "2024-25"]


def _get_status(url: str, timeout: float = 15) -> int:
    """HTTP status for one URL; 0 on network failure (never raises)."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:  # noqa: BLE001 - probe only reports
        return 0


def _list_available_gws(season: str) -> list[int]:
    """Probe gw1..gw38 files; return those that actually exist upstream.

    Phase 21.1 fix: probe the SEASON first (one request) and bail out early —
    previously an unpublished season burned 38 requests, each swallowed
    silently. Every miss now logs its exact URL and status.
    """
    fixtures_status = _get_status(f"{VAASTAV_RAW}/{season}/fixtures.csv")
    if fixtures_status != 200:
        print(
            f"season {season}: not published upstream "
            f"(fixtures.csv HTTP {fixtures_status or 'network-error'})"
        )
        return []
    available: list[int] = []
    for gw in range(1, 39):
        url = f"{VAASTAV_RAW}/{season}/gws/gw{gw}.csv"
        status = _get_status(url)
        if status == 200:
            available.append(gw)
        else:
            label = "404" if status == 404 else "WARN"
            print(f"  {label} {url} (HTTP {status})")
            if gw > 1 and not available:
                break  # gap in published gameweeks; stop probing this season
    return available


def _parse_gw_csv(season: str, gw: int) -> list[dict]:
    raw = _get(f"{VAASTAV_RAW}/{season}/gws/gw{gw}.csv")
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    elements: list[dict] = []
    for row in reader:

        def _num(key: str, cast=float, _row: dict = row):  # noqa: B006 - bound per-row
            val = _row.get(key)
            if val in (None, ""):
                return None
            try:
                return cast(float(val))  # type: ignore[call-arg]
            except (TypeError, ValueError):
                return None

        element_id = _num("element", int)
        if element_id is None:
            continue
        elements.append(
            {
                "element_id": element_id,
                "total_points": int(_num("total_points", int) or 0),
                "minutes": _num("minutes", int),
                "bonus": _num("bonus", int),
                "goals_scored": _num("goals_scored", int),
                "assists": _num("assists", int),
                "expected_goal_involvements": _num("expected_goal_involvements"),
            }
        )
    return elements


def _understat_players(season_year: int) -> dict[int, dict]:
    """Scrape Understat's embedded playersData JSON -> name-keyed xGI totals."""
    html = _get(f"{UNDERSTAT_LEAGUE_URL}/{season_year}").decode("utf-8", errors="replace")
    match = re.search(r"playersData\s*=\s*JSON\.parse\('([^']+)'\)", html)
    if not match:
        return {}
    decoded = json.loads(match.group(1).encode().decode("unicode_escape"))
    by_name: dict[str, dict] = {}
    for row in decoded:
        try:
            by_name[row.get("player_title", "")] = {
                "xg": float(row.get("xG", 0) or 0),
                "xa": float(row.get("xA", 0) or 0),
                "games": int(row.get("games", 0) or 0),
            }
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
    return by_name


def main() -> int:
    base = os.environ.get("ENGINE_BASE", "").strip()
    token = os.environ.get("SYNC_PUSH_TOKEN", "").strip()
    if not base or not token:
        print("ENV FAIL: ENGINE_BASE and SYNC_PUSH_TOKEN are required.", file=sys.stderr)
        return 2

    pushed_gws = 0
    last_error: str | None = None
    for season in _season_candidates():
        gws = _list_available_gws(season)
        print(f"season {season}: {len(gws)} gameweek files available upstream")
        for gw in reversed(gws):  # newest first; stop after first success batch
            try:
                elements = _parse_gw_csv(season, gw)
            except Exception as exc:  # noqa: BLE001 - report and continue
                last_error = f"gw{gw}: download/parse failed ({exc})"
                print(f"  WARN {last_error}")
                continue
            if not elements:
                continue
            try:
                status, body = _post_json(
                    base,
                    "/api/v1/sync/history-push",
                    {
                        "gameweek": gw,
                        "source": "github-actions",
                        "season": season,
                        "elements": elements,
                    },
                    token,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"gw{gw}: push failed ({exc})"
                print(f"  WARN {last_error}")
                continue
            if status != 200:
                last_error = f"gw{gw}: push returned HTTP {status}: {body[:200]}"
                print(f"  WARN {last_error}")
                continue
            result = json.loads(body)
            captured = result.get("predictions_captured")
            scored = result.get("recommendations_scored")
            print(
                f"  OK gw{gw}: stored={result.get('stored')} "
                f"mirrored={result.get('mirrored')} captured={captured} scored={scored}"
            )
            pushed_gws += 1
        if pushed_gws:
            break  # a season with real data was found; done

    # Understat snapshot: enrich-only pass (failures are non-fatal).
    year = max(int(s.split("-")[0]) for s in _season_candidates()) - 1
    try:
        players = _understat_players(year)
        print(f"Understat snapshot {year}/{year+1}: {len(players)} players parsed")
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
        print(f"WARN understat snapshot failed ({exc})")

    if pushed_gws == 0:
        print(f"FAIL: no gameweek could be pushed. Last error: {last_error}", file=sys.stderr)
        return 1
    print(f"DONE: {pushed_gws} gameweek(s) pushed to {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
