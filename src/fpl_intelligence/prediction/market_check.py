"""Phase 23 Gate 0 (C1) — THE single source of truth for the market check.

Three surfaces must render byte-identical market-check text:

* Decisions provenance banner (``report.meta.chain.market_check``),
* Captain Spotlight note (same object, rendered on the captain hero),
* Sources page ``odds_api`` row (``/api/v1/data-sources``).

Every surface calls :func:`compute_market_status` (or reads the payload it
produced at chain-resolution time), so the canonical detail string — e.g.

    matched 10/10 GW2 fixtures · unmatched: LEE, NEW

— can never drift between pages again. A fixture counts as matched when AT
LEAST ONE side resolves against the odds coverage set via
:func:`~fpl_intelligence.data_providers.team_aliases.canonical_team_name`;
clubs whose names are absent from coverage are listed as ``unmatched``
(sorted, capped) even when their fixture still matched through the other side.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from fpl_intelligence.data_providers.team_aliases import canonical_team_name

#: Canonical detail caps so the string stays stable as leagues/seasons grow.
UNMATCHED_CAP = 4


def compute_market_status(
    rows: Sequence[tuple[int | None, int, int]],
    id_to_names: Mapping[int, Iterable[str]],
    covered_names: Iterable[str],
) -> dict[str, Any]:
    """Match fixture rows against odds coverage and build the canonical payload.

    ``rows`` are ``(gameweek, home_team_id, away_team_id)`` triples;
    ``id_to_names`` maps FPL team ids onto every known spelling;
    ``covered_names`` are the bookmaker-side names present in the snapshot
    (already canonicalised via :func:`matched_event_names` or equivalent).

    Semantics (identical to the original Sources probe this module replaced):
    a fixture counts as matched when AT LEAST ONE side resolves into
    coverage; a club whose every spelling misses coverage is listed by its
    alphabetically-first spelling — the 3-letter short code ("LEE").
    """
    # ``covered_names`` are ALREADY canonical (matched_event_names output);
    # re-folding would corrupt multi-word bookmaker names.
    covered = {str(n) for n in covered_names if n}
    resolved: dict[int, tuple[bool, str]] = {}
    for team_id, names in (id_to_names or {}).items():
        spellings = [str(n) for n in (names or []) if n]
        canon_hits = [c for c in (canonical_team_name(s) for s in spellings) if c]
        hit = any(c in covered for c in canon_hits)
        label = min(spellings) if spellings else ""
        resolved[int(team_id)] = (hit, label)

    matched = 0
    unmatched: set[str] = set()
    gameweek: int | None = None
    for row in rows:
        event, home_id, away_id = int(row[0]), int(row[1]), int(row[2])
        gameweek = event if gameweek is None else gameweek
        row_ok = False
        for team_id in (int(home_id), int(away_id)):
            hit, label = resolved.get(team_id, (False, ""))
            if hit:
                row_ok = True
            elif label:
                unmatched.add(label)
        if row_ok:
            matched += 1
    total = len(rows)

    return {
        "fixtures_matched": matched,
        "fixtures_total": total,
        "gameweek": gameweek,
        "unmatched": sorted(unmatched),
        "detail": format_market_detail(
            matched=matched,
            total=total,
            gameweek=gameweek,
            unmatched=sorted(unmatched),
        ),
        "status": (
            "ok"
            if total and matched == total
            else ("degraded" if matched else "blocked")
        ),
    }


def format_market_detail(
    *,
    matched: int,
    total: int,
    gameweek: int | None,
    unmatched: Sequence[str] | None,
) -> str:
    """The ONE canonical market-check sentence every surface renders."""
    gw_txt = f"GW{gameweek}" if gameweek is not None else "GW?"
    detail = f"matched {matched}/{total} {gw_txt} fixtures"
    names = sorted(u for u in (unmatched or []) if u)[:UNMATCHED_CAP]
    if names:
        detail += " · unmatched: " + ", ".join(names)
    return detail


def disabled_market_status(reason: str) -> dict[str, Any]:
    """Honest off-state payload (no key, unreachable, zero matches)."""
    return {
        "enabled": False,
        "reason": reason or "no fixtures matched yet",
        "detail": "",
        "status": "off",
    }


def official_id_names_map(db: Any) -> dict[int, list[str]]:
    """FPL provider team id -> every known spelling (short + full), DB-backed.

    Shared by the Sources probe and the prediction-chain notes so both sides
    resolve team names through one identical map.
    """
    from sqlalchemy import select

    from fpl_intelligence.db.models import Team, TeamExternalId

    mapping: dict[int, list[str]] = {}
    for provider_id, short_name, full_name in db.execute(
        select(TeamExternalId.provider_team_id, Team.short_name, Team.name)
        .join(Team, Team.id == TeamExternalId.team_id)
        .where(TeamExternalId.provider == "official_fpl")
    ).all():
        if provider_id is None:
            continue
        names = [str(c) for c in (short_name, full_name) if c]
        existing = mapping.setdefault(int(provider_id), [])
        for name in names:
            if name not in existing:
                existing.append(name)
    return mapping


# --------------------------------------------------------------------------- #
# Persistence: one shared payload row so surfaces that cannot afford a live
# odds fetch (the materialized fast path) still render THE SAME sentence.
# --------------------------------------------------------------------------- #

_MARKET_CHECK_SOURCE = "market_check"

_CACHE_DDL = (
    "CREATE TABLE IF NOT EXISTS provider_refresh ("
    " source VARCHAR(60) PRIMARY KEY,"
    " season_label VARCHAR(40),"
    " player_count INTEGER NOT NULL DEFAULT 0,"
    " payload JSONB NOT NULL DEFAULT '[]'::jsonb,"
    " fetched_at TIMESTAMP WITH TIME ZONE NOT NULL)"
)


def store_shared_payload(
    db: Any,
    payload: Mapping[str, Any],
    *,
    gameweek: int | None = None,
) -> None:
    """Persist the canonical market-check payload (best-effort, never raises)."""
    try:
        from datetime import UTC, datetime

        from sqlalchemy import select, text

        from fpl_intelligence.sync.materialized_models import ProviderRefreshDB

        db.execute(text(_CACHE_DDL))
        row = db.scalar(
            select(ProviderRefreshDB).where(ProviderRefreshDB.source == _MARKET_CHECK_SOURCE)
        )
        now = datetime.now(UTC)
        merged = dict(payload)
        merged.pop("_gameweek", None)
        if row is None:
            row = ProviderRefreshDB(
                source=_MARKET_CHECK_SOURCE,
                season_label=(f"GW{gameweek}" if gameweek else None),
                player_count=int(payload.get("fixtures_total") or 0),
                payload=merged,
                fetched_at=now,
            )
            db.add(row)
        else:
            row.season_label = f"GW{gameweek}" if gameweek else row.season_label
            row.player_count = int(payload.get("fixtures_total") or 0)
            row.payload = merged
            row.fetched_at = now
        db.commit()
    except Exception:  # noqa: BLE001 — persistence must never break a page
        with contextlib.suppress(Exception):
            db.rollback()


def load_cached_payload(db: Any) -> dict[str, Any] | None:
    """The last stored canonical payload, or ``None``."""
    try:
        from sqlalchemy import select, text

        from fpl_intelligence.sync.materialized_models import ProviderRefreshDB

        db.execute(text(_CACHE_DDL))
        row = db.scalar(
            select(ProviderRefreshDB).where(ProviderRefreshDB.source == _MARKET_CHECK_SOURCE)
        )
    except Exception:  # noqa: BLE001 — status reporting never breaks scoring
        with contextlib.suppress(Exception):
            db.rollback()
        return None
    if row is None or not isinstance(row.payload, dict):
        return None
    return dict(row.payload)
