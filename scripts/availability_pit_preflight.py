"""Read-only preflight for point-in-time FPL availability ingestion.

This script does not write database rows. It fetches a small, deadline-adjacent
sample of Randdalf/fplcache snapshots, verifies their timestamps, parses the FPL
availability fields, and checks canonical player-ID resolution against the live
PostgreSQL database.
"""

from __future__ import annotations

import argparse
import json
import lzma
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from fpl_intelligence.db.models import PlayerExternalId
from fpl_intelligence.db.session import validation_session_factory

FPLCACHE_API = "https://api.github.com/repos/Randdalf/fplcache/contents/cache"
FPLCACHE_RAW = "https://raw.githubusercontent.com/Randdalf/fplcache/main/cache"


def _github_json(url: str) -> object:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "fpl-intelligence-engine"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _snapshot_files(day: date) -> list[tuple[datetime, str]]:
    url = f"{FPLCACHE_API}/{day.year}/{day.month}/{day.day}"
    payload = _github_json(url)
    if not isinstance(payload, list):
        return []
    result: list[tuple[datetime, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name.endswith(".json.xz"):
            continue
        stem = name.removesuffix(".json.xz")
        if len(stem) != 4 or not stem.isdigit():
            continue
        try:
            captured = datetime(
                day.year,
                day.month,
                day.day,
                int(stem[:2]),
                int(stem[2:]),
                tzinfo=UTC,
            )
        except ValueError:
            continue
        result.append((captured, f"{FPLCACHE_RAW}/{day.year}/{day.month}/{day.day}/{name}"))
    return sorted(result)


def _latest_before(cutoff: datetime) -> tuple[datetime, str] | None:
    cutoff = cutoff.astimezone(UTC)
    candidates: list[tuple[datetime, str]] = []
    for offset in (1, 0):
        day = (cutoff - timedelta(days=offset)).date()
        candidates.extend(item for item in _snapshot_files(day) if item[0] <= cutoff)
    return max(candidates, key=lambda item: item[0]) if candidates else None


def _fetch_snapshot(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "fpl-intelligence-engine"})
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    payload = json.loads(lzma.decompress(raw).decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError(f"invalid snapshot payload: {url}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoff", action="append", required=True, help="UTC ISO-8601 cutoff; repeat for multiple samples")
    args = parser.parse_args()

    cutoffs = [datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) for value in args.cutoff]
    samples = []
    for cutoff in cutoffs:
        ref = _latest_before(cutoff)
        if ref is None:
            raise RuntimeError(f"no fplcache snapshot found on or before {cutoff.isoformat()}")
        captured, url = ref
        payload = _fetch_snapshot(url)
        flagged = 0
        player_ids: set[str] = set()
        for element in payload["elements"]:
            status = str(element.get("status") or "a")
            chance = element.get("chance_of_playing_this_round")
            news = str(element.get("news") or "").strip()
            if status != "a" or chance not in (None, 100) or news:
                flagged += 1
                player_ids.add(str(element.get("id")))

        samples.append(
            {
                "cutoff": cutoff.isoformat(),
                "snapshot_captured_at": captured.isoformat(),
                "snapshot_url": url,
                "elements": len(payload["elements"]),
                "flagged_players": flagged,
                "flagged_player_ids": sorted(player_ids),
            }
        )

    with validation_session_factory()() as db:
        canonical = dict(
            db.execute(
                select(PlayerExternalId.provider_player_id, PlayerExternalId.player_id)
                .where(PlayerExternalId.provider.in_(["real_fpl", "official_fpl"]))
            ).all()
        )

    resolved = 0
    total_flagged = 0
    for sample in samples:
        ids = sample["flagged_player_ids"]
        total_flagged += len(ids)
        resolved += sum(1 for pid in ids if pid in canonical)
        sample["canonical_ids_resolved"] = sum(1 for pid in ids if pid in canonical)
        sample["canonical_ids_unresolved"] = sorted(pid for pid in ids if pid not in canonical)

    report = {
        "provider": "fplcache_pit",
        "source": "Randdalf/fplcache",
        "read_only": True,
        "samples": samples,
        "flagged_player_occurrences": total_flagged,
        "resolved_player_occurrences": resolved,
        "resolution_rate": round(resolved / total_flagged, 6) if total_flagged else 1.0,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
