"""Phase 23 Gate 1 (L1) — the LEAGUE KILLER service layer.

Zero config, NO hardcoded league ids: classic leagues are auto-detected from
``/api/entry/{entry}/leagues/`` through the egress masks, cached in Postgres
and refreshed by the daily cron or on demand (10-minute cooldown). The pure
helpers here are unit-testable without network; the async fetchers go through
:class:`FplEgressChain` exactly like every other FPL surface.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Rival picks cap: only the top-10 rivals' picks are fetched/cached.
RIVALS_CAP = 10

#: On-demand refresh cooldown (seconds).
REFRESH_COOLDOWN_SECONDS = 600.0

#: Per-request budget for the whole standings+picks refresh.
_REFRESH_BUDGET_SECONDS = 20.0


def parse_entry_leagues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract CLASSIC leagues from an entry-leagues payload (pure).

    Handles BOTH API shapes:

    * the legacy ``/api/entry/{id}/leagues/`` envelope
      (``{"classic": [...], "h2h": [...]}``), and
    * the 2026/27 shape where the same lists ship INSIDE the entry payload
      itself (``{"id": .., "leagues": {"classic": [...], ...}, ...}``) after
      the standalone leagues route was retired (it now 404s).

    League size comes from ``entry_count`` on the legacy shape or from the
    ``active_phases[*].rank_count`` on the embedded one. H2H and cup
    structures are ignored. Returns a stable list ordered by member count
    (descending) then league id.
    """
    leagues_block = (payload or {}).get("leagues")
    if not isinstance(leagues_block, dict):
        leagues_block = payload or {}
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for lg in leagues_block.get("classic") or []:
        try:
            league_id = int(lg.get("id"))
        except (TypeError, ValueError):
            continue
        if league_id in seen:
            continue
        seen.add(league_id)

        def _int(value: Any) -> int | None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        member_count = _int(lg.get("entry_count"))
        if member_count is None:
            phase_counts = [
                _int(p.get("rank_count"))
                for p in (lg.get("active_phases") or [])
                if isinstance(p, dict)
            ]
            member_count = next((c for c in phase_counts if c), None)
        league_type = str(lg.get("type") or lg.get("league_type") or "").lower()
        out.append(
            {
                "league_id": league_id,
                "name": str(lg.get("name") or f"League {league_id}"),
                "member_count": member_count,
                "entry_rank": _int(lg.get("entry_rank")),
                "entry_last_rank": _int(lg.get("entry_last_rank")),
                # "x" = user-created (private); "s" = system/public.
                "private": league_type == "private" or league_type == "x",
            }
        )
    out.sort(key=lambda x: (-(x["member_count"] or 0), x["league_id"]))
    return out


def pick_default_league(leagues: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Default pick when no choice is remembered: private classic league first.

    Global system leagues — ``Overall`` and any ``Gameweek <n>`` league with
    millions of members — are never the default even though they are the
    biggest. If the user belongs to one or more private (``x``) classic
    leagues we pick the biggest private one; otherwise we pick the biggest
    non-global league; if every league is global we fall back to the biggest
    global so a non-empty input never yields None.
    """
    if not leagues:
        return None

    def _is_global(lg: dict[str, Any]) -> bool:
        name = str(lg.get("name") or "").strip().lower()
        return name == "overall" or name.startswith("gameweek")

    private = [lg for lg in leagues if lg.get("private")]
    if private:
        return sorted(
            private, key=lambda x: (-(x.get("member_count") or 0), x["league_id"])
        )[0]
    non_global = [lg for lg in leagues if not _is_global(lg)]
    candidates = non_global if non_global else leagues
    return sorted(
        candidates, key=lambda x: (-(x.get("member_count") or 0), x["league_id"])
    )[0]


def ownership_insights(
    user_ids: set[int],
    rival_picks: dict[str, list[int]],
    name_map: dict[int, str],
    *,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Ownership heat between the user's squad and the capped rivals (pure).

    Returns the most-differentially-owned players both ways:
    ``{"player_id", "web_name", "rival_owners", "rivals", "user_owns"}``.
    """
    total_rivals = len(rival_picks)
    if not total_rivals:
        return []
    counts: dict[int, int] = {}
    for ids in rival_picks.values():
        for pid in ids:
            counts[int(pid)] = counts.get(int(pid), 0) + 1
    insights: list[dict[str, Any]] = []
    for pid, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        owns = int(pid) in user_ids
        insights.append(
            {
                "player_id": pid,
                "web_name": name_map.get(int(pid), f"Player {pid}"),
                "rival_owners": n,
                "rivals": total_rivals,
                "user_owns": owns,
                "line": (
                    f"{n} of {total_rivals} top rivals own "
                    f"{name_map.get(int(pid), f'Player {pid}')}"
                    + (" — you don't" if not owns else "")
                ),
            }
        )
    # Most-shared first, but surface the "you don't own it" ones preferentially.
    insights.sort(key=lambda i: (i["user_owns"], -i["rival_owners"]))
    return insights[:top_n]


def projected_edge_lines(
    rec_xi: list[int],
    xpts_by_element: dict[int, float],
    rival_xis: dict[str, list[int]],
    rival_names: dict[str, str],
) -> dict[str, Any]:
    """User recommended-XI xPTS vs each top rival's XI xPTS (pure).

    Missing predictions simply score as 0 — never invented numbers; lines are
    sorted worst-gap-first so the sharpest edge leads.
    """
    user_total = round(sum(float(xpts_by_element.get(p, 0.0)) for p in rec_xi), 2)
    lines: list[dict[str, Any]] = []
    for entry_key, xi in rival_xis.items():
        rival_total = round(
            sum(float(xpts_by_element.get(p, 0.0)) for p in xi), 2
        )
        gap = round(user_total - rival_total, 2)
        lines.append(
            {
                "entry_id": entry_key,
                "entry_name": rival_names.get(entry_key, f"Entry {entry_key}"),
                "your_xpts": user_total,
                "rival_xpts": rival_total,
                "gap": gap,
                "line": f"{gap:+.1f} vs {rival_names.get(entry_key, entry_key)}",
            }
        )
    lines.sort(key=lambda line: line["gap"])
    return {"lines": lines, "your_xpts": user_total}  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Async fetchers (egress-mask backed) + cache orchestration
# --------------------------------------------------------------------------- #


def _chain(cache_ttl: float = 300.0, *, timeout: float | None = None) -> Any:
    from fpl_intelligence.config import get_settings
    from fpl_intelligence.data_providers.fpl_egress import FplEgressChain

    settings = get_settings()
    return FplEgressChain(
        settings.fpl_base_url,
        timeout=timeout if timeout is not None else settings.egress_strategy_timeout,
        cache_ttl=cache_ttl,
    )


async def fetch_entry_leagues(entry_id: int) -> list[dict[str, Any]]:
    """Live classic-league discovery for one entry via the masks.

    Reads ``leagues.classic`` from the entry payload itself — the 2026/27
    API retired the standalone ``/api/entry/{id}/leagues/`` route (it 404s).
    """
    chain = _chain(cache_ttl=600.0)
    payload = await chain.fetch(f"/api/entry/{int(entry_id)}/")
    return parse_entry_leagues(payload)


async def fetch_standings(league_id: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Standings page 1 plus league meta via the masks."""
    chain = _chain(cache_ttl=300.0)
    payload = await chain.fetch(
        f"/api/leagues-classic/{int(league_id)}/standings/?page_standings=1"
    )
    league_meta = payload.get("league")
    if isinstance(league_meta, dict):
        name = str(league_meta.get("name") or f"League {league_id}")
        member_count = league_meta.get("rank_count")
    else:
        name = str(league_meta or "").strip() or f"League {league_id}"
        member_count = payload.get("total")
    meta = {"name": name, "member_count": member_count}
    results = ((payload.get("standings") or {}).get("results")) or []
    rows: list[dict[str, Any]] = []
    for r in results:
        try:
            entry_id = int(r.get("entry"))
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "rank": r.get("rank"),
                "last_rank": r.get("last_rank"),
                "entry_id": entry_id,
                "entry_name": str(r.get("entry_name") or f"Entry {entry_id}"),
                "player_name": str(r.get("player_name") or ""),
                "total": r.get("total"),
                "gw_points": r.get("event_total"),
            }
        )
    return rows, meta


#: Per-strategy timeout for rival-picks pulls — mirrors the matchday live
#: poll so ten parallel picks still fit inside one serverless request.
PICKS_STRATEGY_TIMEOUT = 1.6


async def fetch_entry_picks(entry_id: int, gameweek: int) -> dict[str, Any]:
    """Starters + captain element ids for one entry's gameweek."""
    chain = _chain(cache_ttl=300.0, timeout=PICKS_STRATEGY_TIMEOUT)
    payload = await chain.fetch(
        f"/api/entry/{int(entry_id)}/event/{int(gameweek)}/picks/"
    )
    picks = payload.get("picks") or []
    starters: list[int] = []
    captain: int | None = None
    for p in picks[:15]:
        try:
            eid = int(p.get("element"))
        except (TypeError, ValueError):
            continue
        position = p.get("position")
        if isinstance(position, int):
            if position <= 11:
                starters.append(eid)
        elif len(starters) < 11:
            # No position info — treat the first 11 as starters.
            starters.append(eid)
        if p.get("is_captain"):
            captain = eid
    return {"starters": starters, "captain": captain}


async def refresh_league_cache(
    db: Session,
    league_id: int,
    gameweek: int,
    *,
    include_picks: bool = True,
) -> dict[str, Any]:
    """Fetch + persist standings page 1 and capped rival picks.

    Honesty contract: leagues larger than the standings page carry a
    ``partial`` note; picks cover at most :data:`RIVALS_CAP` rivals.
    """
    from fpl_intelligence.leagues.models import LeagueCacheDB

    started = time.monotonic()
    rows, meta = await fetch_standings(league_id)

    rivals_picks: dict[str, Any] = {
        "gameweek": int(gameweek),
        "picks": {},
        "captains": {},
        "cap": RIVALS_CAP,
        "partial": False,
    }
    if include_picks:
        # Rivals ranked ABOVE-or-near the user matter most; take the first
        # RIVALS_CAP entries of the page (page 1 is rank-ordered anyway).
        targets = [r for r in rows if r["entry_id"]][:RIVALS_CAP]
        sem = asyncio.Semaphore(4)

        async def _one(eid: int) -> tuple[str, dict[str, Any]]:
            """Picks for ``gameweek``, falling back to the previous one.

            Before a deadline FPL answers 404 for the upcoming gameweek's
            picks, so rivals are profiled on the newest COMPLETED week.
            """
            async with sem:
                for candidate_gw in (int(gameweek), int(gameweek) - 1):
                    if candidate_gw < 1:
                        break
                    try:
                        block = await fetch_entry_picks(eid, candidate_gw)
                    except Exception:  # noqa: BLE001 — masks fail per-call
                        continue
                    if block.get("starters"):
                        return str(eid), {**block, "gw": candidate_gw}
                return str(eid), {}

        results = await asyncio.wait_for(
            asyncio.gather(*(_one(r["entry_id"]) for r in targets)),
            timeout=max(4.0, _REFRESH_BUDGET_SECONDS - (time.monotonic() - started)),
        )
        picks_map: dict[str, list[int]] = {}
        captains_map: dict[str, int] = {}
        gws_used: dict[int, int] = {}
        fetched = 0
        for key, block in results:
            if not block:
                continue
            picks_map[key] = block.get("starters") or []
            if block.get("captain"):
                captains_map[key] = int(block["captain"])
            gws_used[int(block.get("gw") or gameweek)] = (
                gws_used.get(int(block.get("gw") or gameweek), 0) + 1
            )
            fetched += 1
        rivals_picks["picks"] = picks_map
        rivals_picks["captains"] = captains_map
        rivals_picks["fetched"] = fetched
        # The gameweek most rivals' picks actually describe.
        if gws_used:
            rivals_picks["gameweek"] = max(gws_used.items(), key=lambda kv: kv[1])[0]
        rivals_picks["partial"] = bool(
            meta.get("member_count")
            and int(meta["member_count"] or 0) > len(rows)
        )

    now = datetime.now(UTC)
    row = db.get(LeagueCacheDB, int(league_id))
    if row is None:
        row = LeagueCacheDB(league_id=int(league_id), refreshed_at=now)
        db.add(row)
    row.name = meta["name"]
    row.member_count = meta.get("member_count")
    row.standings = rows
    row.rivals_picks = rivals_picks
    row.refreshed_at = now
    db.commit()
    return {"ok": True, "rows": len(rows), "meta": meta}


def upsert_entry_leagues(db: Session, entry_id: int, leagues: list[dict[str, Any]]) -> int:
    """Persist ALL discovered classic leagues for one entry (idempotent)."""
    from fpl_intelligence.leagues.models import EntryLeagueDB

    now = datetime.now(UTC)
    existing = {
        int(row.league_id)
        for row in db.execute(
            select(EntryLeagueDB).where(EntryLeagueDB.entry_id == int(entry_id))
        ).scalars().all()
    }
    added = 0
    for lg in leagues:
        if lg["league_id"] in existing:
            row = db.scalar(
                select(EntryLeagueDB).where(
                    EntryLeagueDB.entry_id == int(entry_id),
                    EntryLeagueDB.league_id == int(lg["league_id"]),
                )
            )
            row.entry_rank = lg.get("entry_rank")
            row.member_count = lg.get("member_count")
            continue
        db.add(
            EntryLeagueDB(
                entry_id=int(entry_id),
                league_id=int(lg["league_id"]),
                league_name=lg["name"],
                member_count=lg.get("member_count"),
                entry_rank=lg.get("entry_rank"),
                entry_last_rank=lg.get("entry_last_rank"),
                private=bool(lg.get("private")),
                discovered_at=now,
            )
        )
        added += 1
    db.commit()
    return added


def stored_entry_leagues(db: Session, entry_id: int) -> list[dict[str, Any]]:
    from fpl_intelligence.leagues.models import EntryLeagueDB

    rows = db.execute(
        select(EntryLeagueDB)
        .where(EntryLeagueDB.entry_id == int(entry_id))
        .order_by(EntryLeagueDB.member_count.desc().nulls_last())
    ).scalars().all()
    return [
        {
            "league_id": r.league_id,
            "name": r.league_name,
            "member_count": r.member_count,
            "entry_rank": r.entry_rank,
            "private": r.private,
        }
        for r in rows
    ]
