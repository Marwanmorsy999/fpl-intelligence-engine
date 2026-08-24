"""Phase 24 Gate 1 C2 — set-piece taker curator.

Manual curation: data/set_piece_takers.json with {team_id: {penalty, corners, free_kicks}}.
Seed with known 2026/27 takers; honest 'unknown' for unmapped teams.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CANDIDATES = [
    Path("data/set_piece_takers.json"),
    Path(__file__).resolve().parents[2] / "data" / "set_piece_takers.json",
    Path(__file__).resolve().parents[3] / "data" / "set_piece_takers.json",
]

_raw_map: dict[int, dict[str, int]] | None = None

def _load_raw() -> dict[int, dict[str, int]]:
    global _raw_map
    if _raw_map is not None:
        return _raw_map
    for cand in _CANDIDATES:
        try:
            if cand.is_file():
                raw = json.loads(cand.read_text(encoding="utf-8"))
                out: dict[int, dict[str, int]] = {}
                for k, v in raw.items():
                    try:
                        tid = int(k)
                        if isinstance(v, dict):
                            entry: dict[str, int] = {}
                            for sk, sv in v.items():
                                if sv is None:
                                    continue
                                entry[sk] = int(sv)
                            out[tid] = entry
                    except Exception:
                        continue
                _raw_map = out
                return out
        except Exception as exc:
            logger.debug("set-piece map load failed %s: %s", cand, exc)
    _raw_map = {}
    return _raw_map

def set_piece_flags(player_id: int, team_id: int | None) -> dict[str, Any]:
    """Return {penalty, corners, free_kicks, unknown} for a player.

    Unknown is True when the team is not curated.
    """
    raw = _load_raw()
    if team_id is None or int(team_id) not in raw:
        return {"penalty": False, "corners": False, "free_kicks": False, "unknown": True}
    entry = raw[int(team_id)]
    pid = int(player_id)
    return {
        "penalty": entry.get("penalty") == pid,
        "corners": entry.get("corners") == pid,
        "free_kicks": entry.get("free_kicks") == pid,
        "unknown": False,
    }

def is_any_taker(player_id: int, team_id: int | None) -> bool:
    flags = set_piece_flags(player_id, team_id)
    return bool(flags.get("penalty") or flags.get("corners") or flags.get("free_kicks"))
