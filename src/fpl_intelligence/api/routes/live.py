"""Phase 20.4 — live matchday engine.

``GET /api/v1/live?session_id=<entry>`` answers one question on matchday:
how is MY squad doing RIGHT NOW.

Data path (all through the Phase 18 egress masks so Vercel's blocked IPs
never produce a hard failure):

* current GW      — ``/api/bootstrap-static/`` (in-process cache 10 min)
* LIVE MODE       — any PL kickoff inside ``now - 2h .. now + 2h``, read from
                    the materialized ``fixtures_cache`` table (zero egress)
* live points     — ``/api/event/{gw}/live/`` (cache 90 s, TOTAL budget 6 s)
* user picks      — ``/api/entry/{entry}/event/{gw}/picks/`` (cache 5 min)
* cross-check     — ESPN eng.1 scoreboard strip (no key, cache 90 s)

Every successful assembly is persisted as a snapshot row; when every mask
fails the endpoint serves the last snapshot with an honest age instead of a
blank page. When the gameweek is finished the response says so, lazy-grades
any ungraded recommendations for that GW, and links Track Record.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select, text

from fpl_intelligence.api import deps
from fpl_intelligence.config import get_settings
from fpl_intelligence.sync.materialized_models import LiveSnapshotDB
from fpl_intelligence.sync.service import score_pending_recommendations

router = APIRouter(tags=["live"])
logger = logging.getLogger(__name__)

GetDB = deps.GetDB

#: Kickoff window that flips the page into LIVE mode (hours either side).
LIVE_WINDOW_HOURS = 2.0

_LIVE_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS live_event_log (
    id SERIAL PRIMARY KEY,
    gameweek INTEGER NOT NULL,
    element_id INTEGER NOT NULL,
    event_kind VARCHAR(20) NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0,
    notified_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT uq_live_event UNIQUE (gameweek, element_id, event_kind, ordinal)
)
"""

#: Phase 23 (L4): stat fields whose increase becomes a matchday ping.
_EVENT_KINDS = (("goals", "goal", "⚽"), ("assists", "assist", "🎯"),
                ("red_cards", "red_card", "🟥"))


def detect_stat_events(
    current: dict[int, dict[str, Any]],
    previous: dict[int, dict[str, Any]],
    *,
    watched_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """New goals/assists/red-cards between two live-stats snapshots (pure).

    Each event carries ``ordinal`` = the player's new CUMULATIVE count of that
    stat, which doubles as the per-event dedupe key. ``points_delta`` comes
    from the official live-points totals. Players outside ``watched_ids``
    (e.g. rivals) are ignored.
    """
    events: list[dict[str, Any]] = []
    for eid, now in current.items():
        if watched_ids is not None and int(eid) not in watched_ids:
            continue
        was = previous.get(int(eid)) or {}
        for field, kind, _emoji in _EVENT_KINDS:
            cur_n = int(now.get(field) or 0)
            old_n = int(was.get(field) or 0)
            for ordinal in range(old_n + 1, cur_n + 1):
                events.append(
                    {
                        "element_id": int(eid),
                        "kind": kind,
                        "ordinal": ordinal,
                        "minute": int(now.get("minutes") or 0),
                        "points_delta": round(
                            float(now.get("points") or 0)
                            - float(was.get("points") or 0),
                            2,
                        ),
                    }
                )
    return events


def event_message(
    event: dict[str, Any],
    name_map: dict[int, str],
    *,
    captain_id: int | None,
) -> str:
    """One honest ping line, e.g. "⚽ Haaland +6 (62') — captain delta +12".

    Captain deltas double the underlying swing because the armband doubles
    the player's points.
    """
    emojis = {"goal": "⚽", "assist": "🎯", "red_card": "🟥"}
    name = name_map.get(int(event["element_id"]), f"Player {event['element_id']}")
    delta = int(round(float(event.get("points_delta") or 0)))
    line = f"{emojis[event['kind']]} {name} {delta:+d} ({event['minute']}')"
    if captain_id is not None and int(captain_id) == int(event["element_id"]) \
            and event["kind"] != "red_card":
        line += f" — captain delta {delta * 2:+d}"
    elif captain_id is not None and int(captain_id) == int(event["element_id"]):
        line += " — CAPTAIN"
    return line

#: In-process cache TTLs (seconds).
BOOTSTRAP_TTL = 600.0
PICKS_TTL = 300.0
LIVE_TTL = 90.0
ESPN_TTL = 90.0

#: Per-strategy network timeout for the live poll. Four strategies x 1.5 s
#: keeps the TOTAL worst case at the required 6-second budget.
LIVE_STRATEGY_TIMEOUT = 1.5

ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"
)

_POSITION_NAMES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# --------------------------------------------------------------------------- #
# Self-sealing DDL for the snapshot table (prod DB predates the model).
# --------------------------------------------------------------------------- #

_LIVE_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS live_snapshots (
    id SERIAL PRIMARY KEY,
    gameweek INTEGER NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at TIMESTAMP WITH TIME ZONE NOT NULL
)
"""
_ensure_lock = threading.Lock()
_ensured = False


def _ensure_live_tables(db: Any) -> None:
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        try:
            db.execute(text(_LIVE_SNAPSHOT_DDL))
            db.commit()
        except Exception as exc:  # noqa: BLE001 — sqlite tests pre-create it
            db.rollback()
            logger.debug("live_snapshots DDL skipped: %s", exc)
        _ensured = True


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without network)
# --------------------------------------------------------------------------- #


def _parse_kickoff(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _in_kickoff_window(
    now_utc: datetime,
    kickoff_times: list[datetime],
    *,
    window_hours: float = LIVE_WINDOW_HOURS,
) -> bool:
    """True when any kickoff lies within ±window hours of now."""
    span = timedelta(hours=window_hours)
    return any(abs(now_utc - ko) <= span for ko in kickoff_times)


def _current_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The bootstrap event that is 'now': is_current first, else first unfinished."""
    for ev in events:
        if ev.get("is_current"):
            return ev
    for ev in sorted(events, key=lambda e: e.get("id") or 0):
        if not ev.get("finished"):
            return ev
    return events[-1] if events else None


def _element_points(el: dict[str, Any]) -> float:
    """Official live points for one element: stats.total_points first,
    explain-sum as a defensive fallback."""
    stats = el.get("stats") or {}
    tp = stats.get("total_points")
    if isinstance(tp, (int, float)):
        return float(tp)
    total = 0.0
    for fixture_explain in el.get("explain") or []:
        for stat in fixture_explain.get("stats") or []:
            pts = stat.get("points")
            if isinstance(pts, (int, float)):
                total += float(pts)
    return round(total, 2)


def _pick_index(picks: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """element_id -> pick metadata (position/captaincy/multiplier)."""
    out: dict[int, dict[str, Any]] = {}
    for p in picks or []:
        eid = p.get("element")
        if eid is None:
            continue
        out[int(eid)] = {
            "position": int(p.get("position") or 99),
            "is_captain": bool(p.get("is_captain")),
            "is_vice_captain": bool(p.get("is_vice_captain")),
            "multiplier": int(p.get("multiplier") or 1),
        }
    return out


def _espn_matches(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact scoreboard rows from the ESPN open API."""
    matches: list[dict[str, Any]] = []
    for ev in (payload or {}).get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        status = ev.get("status") or {}
        stype = status.get("type") or {}
        matches.append(
            {
                "short": ev.get("shortName") or ev.get("name") or "",
                "state": stype.get("state") or "",
                "detail": stype.get("shortDetail") or "",
                "home_abbr": ((home.get("team") or {}).get("abbreviation")) or "",
                "home_score": home.get("score"),
                "away_abbr": ((away.get("team") or {}).get("abbreviation")) or "",
                "away_score": away.get("score"),
                "clock": status.get("displayClock") or "",
            }
        )
    return matches


def _mask_status(
    exc: Exception | None = None,
    *,
    strategy: str | None = None,
    ok: bool = False,
    skipped: bool = False,
) -> dict[str, Any]:
    if skipped:
        return {"status": "skipped", "strategy": None, "error": None}
    if ok:
        return {"status": "ok", "strategy": strategy, "error": None}
    return {
        "status": "fail",
        "strategy": strategy,
        "error": f"{type(exc).__name__}: {exc}" if exc else "failed",
    }


# --------------------------------------------------------------------------- #
# Egress chains (one per TTL class) + ESPN strip cache
# --------------------------------------------------------------------------- #

_chain_lock = threading.Lock()
_chains: dict[str, tuple[Any, Any]] = {}


def _chain(kind: str) -> tuple[Any, Any]:
    """(chain, ttl) for 'bootstrap' | 'picks' | 'live'."""
    settings = get_settings()
    from fpl_intelligence.data_providers.fpl_egress import FplEgressChain

    specs = {
        "bootstrap": (settings.egress_strategy_timeout, BOOTSTRAP_TTL),
        "picks": (settings.egress_strategy_timeout, PICKS_TTL),
        "live": (LIVE_STRATEGY_TIMEOUT, LIVE_TTL),
    }
    timeout, ttl = specs[kind]
    with _chain_lock:
        hit = _chains.get(kind)
        if hit is None or hit[1] != ttl:
            chain = FplEgressChain(settings.fpl_base_url, timeout=timeout, cache_ttl=ttl)
            _chains[kind] = (chain, ttl)
        return _chains[kind]


_espn_cache: tuple[float, list[dict[str, Any]] | None] = (0.0, None)


async def _espn_strip() -> tuple[list[dict[str, Any]] | None, str | None]:
    """ESPN eng.1 scoreboard (no key). Returns (matches, error)."""
    global _espn_cache
    now_mono = time.monotonic()
    if _espn_cache[1] is not None and now_mono - _espn_cache[0] < ESPN_TTL:
        return _espn_cache[1], None
    try:
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
            r = await client.get(ESPN_SCOREBOARD_URL)
            r.raise_for_status()
            matches = _espn_matches(r.json())
    except Exception as exc:  # noqa: BLE001 — the strip enriches, never blocks
        return None, f"{type(exc).__name__}: {exc}"
    _espn_cache = (now_mono, matches)
    return matches, None


# --------------------------------------------------------------------------- #
# Row assembly
# --------------------------------------------------------------------------- #


def _build_rows(
    squad_players: list[dict[str, Any]],
    live_stats: dict[int, dict[str, Any]],
    name_map: dict[int, str],
    team_map: dict[int, str],
    elem_meta: dict[int, dict[str, Any]],
    pos_map: dict[int, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(starters, bench) rows; captain multiplier already applied."""
    starters: list[dict[str, Any]] = []
    bench: list[dict[str, Any]] = []
    for meta in sorted(squad_players, key=lambda m: m["position"]):
        eid = int(meta["element_id"])
        st = live_stats.get(eid)
        raw_pts = float(st["points"]) if st else None
        mult = int(meta.get("multiplier") or 1)
        em = elem_meta.get(eid) or {}
        row = {
            "element_id": eid,
            "name": name_map.get(eid, f"Player {eid}"),
            "team": team_map.get(em.get("team", 0), ""),
            "team_id": em.get("team", 0),
            "pos": pos_map.get(em.get("pos", 0), ""),
            "minutes": st["minutes"] if st else None,
            "goals": st["goals"] if st else None,
            "assists": st["assists"] if st else None,
            "red_cards": st["red_cards"] if st else None,
            "bonus": st["bonus"] if st else None,
            "raw_points": raw_pts,
            "multiplier": mult,
            "points": round(raw_pts * mult, 2) if raw_pts is not None else None,
            "is_captain": bool(meta.get("is_captain")),
            "is_vice": bool(meta.get("is_vice_captain")),
        }
        (bench if int(meta["position"]) > 11 else starters).append(row)
    return starters, bench


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #


@router.get("/live")
async def live_matchday(
    response: Response,
    db: GetDB,
    session_id: str | None = Query(None, description="FPL entry id (the saved session key)."),
) -> dict[str, Any]:
    """Live matchday board: rows, captain-vs-vice headline, honest staleness."""
    response.headers["Cache-Control"] = "no-store"
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id query parameter is required")

    _ensure_live_tables(db)

    masks: dict[str, dict[str, Any]] = {}
    note_parts: list[str] = []

    # --- 1. bootstrap: current GW + name/team/pos maps ------------------------
    bootstrap: dict[str, Any] | None = None
    try:
        chain, _ttl = _chain("bootstrap")
        bootstrap = await chain.fetch("/api/bootstrap-static/")
        masks["bootstrap"] = _mask_status(ok=True, strategy=chain.winning_strategy)
    except Exception as exc:  # noqa: BLE001
        masks["bootstrap"] = _mask_status(exc)
        note_parts.append("bootstrap unavailable")

    events = (bootstrap or {}).get("events") or []
    cur_ev = _current_event(events) if events else None
    gameweek = int(cur_ev.get("id")) if cur_ev else 1
    gw_finished = bool((cur_ev or {}).get("finished"))

    name_map: dict[int, str] = {}
    team_map: dict[int, str] = {}
    pos_map: dict[int, str] = {}
    elem_meta: dict[int, dict[str, Any]] = {}
    for e in (bootstrap or {}).get("elements") or []:
        try:
            eid = int(e["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name_map[eid] = str(e.get("web_name") or "?")
        elem_meta[eid] = {"team": int(e.get("team") or 0), "pos": int(e.get("element_type") or 0)}
    for t in (bootstrap or {}).get("teams") or []:
        try:
            team_map[int(t["id"])] = str(t.get("short_name") or "?")
        except (KeyError, TypeError, ValueError):
            continue
    for et in (bootstrap or {}).get("element_types") or []:
        try:
            tid = int(et["id"])
        except (KeyError, TypeError, ValueError):
            continue
        pos_map[tid] = _POSITION_NAMES.get(tid, "?")

    # --- 2. LIVE MODE from materialized fixtures ------------------------------
    from fpl_intelligence.fixtures.scanner import parse_fixtures
    from fpl_intelligence.sync.materialized_models import FixturesCacheDB

    fx_row = db.scalar(select(FixturesCacheDB).order_by(FixturesCacheDB.id.desc()).limit(1))
    kickoffs: list[datetime] = []
    fixtures_fresh = fx_row is not None
    if fx_row is not None:
        for row in parse_fixtures(fx_row.payload or []):
            if row.event != gameweek or row.finished:
                continue
            ko = _parse_kickoff(row.kickoff)
            if ko is not None:
                kickoffs.append(ko)
    now_utc = datetime.now(UTC)
    live_mode = _in_kickoff_window(now_utc, kickoffs)
    if not fixtures_fresh:
        note_parts.append("fixtures cache empty — run the daily job for LIVE MODE")

    # --- 3. user picks ----------------------------------------------------------
    picks_source = ""
    squad_players: list[dict[str, Any]] = []
    picks_payload: dict[str, Any] | None = None
    try:
        # v2.7.6-session-guard: never build an entry URL from a non-numeric id.
        if not str(session_id).strip().isdigit():
            raise ValueError("session_id is not a numeric FPL entry id")
        chain, _ttl = _chain("picks")
        picks_payload = await chain.fetch(
            f"/api/entry/{int(session_id)}/event/{gameweek}/picks/"
        )
        masks["picks"] = _mask_status(ok=True, strategy=chain.winning_strategy)
        picks_source = "fpl-picks"
    except Exception as exc:  # noqa: BLE001
        masks["picks"] = _mask_status(exc)

    if picks_payload and picks_payload.get("picks"):
        idx = _pick_index(picks_payload["picks"])
        squad_players = [
            {"element_id": eid, **meta}
            for eid, meta in sorted(idx.items(), key=lambda kv: kv[1]["position"])
        ]
    else:
        squad = _load_saved_squad(db, session_id)
        if squad is not None:
            ids = list(squad.player_ids)
            cap_id = getattr(squad, "captain_id", None)
            vice_id = getattr(squad, "vice_captain_id", None)
            squad_players = [
                {
                    "element_id": int(pid),
                    "position": i + 1,
                    "is_captain": int(pid) == int(cap_id) if cap_id else False,
                    "is_vice_captain": int(pid) == int(vice_id) if vice_id else False,
                    "multiplier": 2 if (cap_id and int(pid) == int(cap_id)) else 1,
                }
                for i, pid in enumerate(ids)
            ]
            picks_source = "saved-squad"

    # --- 4. live points -----------------------------------------------------------
    live_payload: dict[str, Any] | None = None
    if not gw_finished:
        try:
            chain, _ttl = _chain("live")
            live_payload = await chain.fetch(f"/api/event/{gameweek}/live/")
            masks["live"] = _mask_status(ok=True, strategy=chain.winning_strategy)
        except Exception as exc:  # noqa: BLE001
            masks["live"] = _mask_status(exc)
            note_parts.append("live feed failed")
    else:
        masks["live"] = _mask_status(skipped=True)

    live_stats: dict[int, dict[str, Any]] = {}
    for el in (live_payload or {}).get("elements") or []:
        try:
            eid = int(el["id"])
        except (KeyError, TypeError, ValueError):
            continue
        stats = el.get("stats") or {}
        live_stats[eid] = {
            "minutes": int(stats.get("minutes") or 0),
            "goals": int(stats.get("goals_scored") or 0),
            "assists": int(stats.get("assists") or 0),
            "bonus": int(stats.get("bonus") or 0),
            "red_cards": int(stats.get("red_cards") or 0),
            "points": _element_points(el),
        }

    starters, bench = _build_rows(
        squad_players, live_stats, name_map, team_map, elem_meta, pos_map
    )

    team_total = sum(float(r["points"] or 0) for r in starters)

    cap_row = next((r for r in starters if r["is_captain"]), None)
    vice_row = next((r for r in bench + starters if r["is_vice"]), None)
    cap_pts = float(cap_row["points"] or 0) if cap_row else 0.0
    vice_doubled = float((vice_row["raw_points"] or 0) * 2) if vice_row else 0.0
    delta = round(cap_pts - vice_doubled, 2)

    headline_text = f"Team {team_total:.0f} pts · Captain vs Vice: {delta:+.0f}"

    # --- 5. ESPN cross-check strip ----------------------------------------------
    espn_matches, espn_error = await _espn_strip()
    masks["espn"] = (
        {"status": "ok", "strategy": "direct", "error": None}
        if espn_matches is not None
        else {"status": "fail", "strategy": "direct", "error": espn_error}
    )

    # --- 6. matchday pings + snapshot persistence / stale fallback ------------
    data_age = 0.0
    stale_snapshot = False
    assembled_ok = bool(rows_have_data(starters)) and (gw_finished or live_payload is not None)
    pings_sent = 0

    # Phase 23 (L4): diff THIS assembly vs the last stored snapshot and emit
    # deduped goal/assist/red-card pings for fielded players.
    if assembled_ok and squad_players:
        try:
            last_snap_row = db.scalar(
                select(LiveSnapshotDB)
                .where(LiveSnapshotDB.gameweek == gameweek)
                .order_by(LiveSnapshotDB.id.desc())
                .limit(1)
            )
            previous_stats: dict[int, dict[str, Any]] = {}
            if last_snap_row is not None:
                sp = last_snap_row.payload or {}
                for r in list(sp.get("rows_all") or []) + list(sp.get("bench") or []):
                    if isinstance(r, dict) and r.get("element_id"):
                        previous_stats[int(r["element_id"])] = {
                            "goals": r.get("goals"),
                            "assists": r.get("assists"),
                            "red_cards": r.get("red_cards"),
                            "minutes": r.get("minutes"),
                            "points": r.get("raw_points"),
                        }
            watched = {
                int(m["element_id"])
                for m in squad_players
                if int(m.get("multiplier") or 0) > 0
            }
            current_stats = {
                int(eid): {
                    "goals": st["goals"],
                    "assists": st["assists"],
                    "red_cards": st["red_cards"],
                    "minutes": st["minutes"],
                    "points": st["points"],
                }
                for eid, st in live_stats.items()
            }
            events = detect_stat_events(current_stats, previous_stats, watched_ids=watched)
            if events:
                name_map_ping = dict(name_map)
                captain_pid = next(
                    (int(m["element_id"]) for m in squad_players if m.get("is_captain")),
                    None,
                )
                pings_sent = _emit_matchday_pings(
                    db, gameweek, session_id, events,
                    name_map_ping, captain_pid,
                )
        except Exception as exc:  # noqa: BLE001 — pings never break the board
            db.rollback()
            logger.debug("matchday pings failed: %s", exc)

    if assembled_ok:
        try:
            db.add(
                LiveSnapshotDB(
                    gameweek=gameweek,
                    payload=_snapshot_payload(
                        starters, bench, headline_text, team_total, espn_matches
                    ),
                    fetched_at=datetime.now(UTC),
                )
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 — snapshots never block serving
            db.rollback()
            logger.debug("snapshot write failed: %s", exc)
    elif not assembled_ok:
        # Masks failed mid-flight: backfill from the last stored snapshot so the
        # page never renders blank, with an honest age attached.
        last = db.scalar(
            select(LiveSnapshotDB)
            .where(LiveSnapshotDB.gameweek == gameweek)
            .order_by(LiveSnapshotDB.id.desc())
            .limit(1)
        )
        if last is not None:
            sp = last.payload or {}
            fetched = last.fetched_at
            if fetched.tzinfo is None:  # sqlite returns naive UTC
                fetched = fetched.replace(tzinfo=UTC)
            data_age = max(0.0, (datetime.now(UTC) - fetched).total_seconds())
            stale_snapshot = True
            snap_starters = {r["element_id"]: r for r in sp.get("rows_all") or []}
            snap_bench = {r["element_id"]: r for r in sp.get("bench") or []}
            for target, source_map in ((starters, snap_starters), (bench, snap_bench)):
                for r in target:
                    sr = source_map.get(r["element_id"])
                    if sr is None:
                        continue
                    for field in ("points", "minutes", "goals", "assists", "bonus"):
                        if r.get(field) is None and sr.get(field) is not None:
                            r[field] = sr[field]
            team_total = sum(float(r["points"] or 0) for r in starters)
            headline_text = f"Team {team_total:.0f} pts · Captain vs Vice: {delta:+.0f}"
            if not espn_matches and sp.get("espn_matches"):
                espn_matches = sp.get("espn_matches")
                masks["espn"] = {"status": "snapshot", "strategy": "cache", "error": None}
            mins = int(data_age / 60)
            note_parts.insert(
                0,
                f"live feed unavailable — last snapshot {mins} min old; Retry for fresh",
            )

    # --- 7. GW finished: lazy grading ----------------------------------------------
    graded_now = 0
    if gw_finished:
        try:
            graded_now = score_pending_recommendations(db, up_to_gameweek=gameweek)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("lazy grading failed: %s", exc)
        note_parts.append(
            f"GW{gameweek} complete — final points; Track Record "
            + ("graded just now" if graded_now else "already graded")
        )
        headline_text = f"GW{gameweek} complete — final {team_total:.0f} pts"

    return {
        "session_id": session_id,
        "gameweek": gameweek,
        "live_mode": live_mode,
        "gw_finished": gw_finished,
        "headline": {
            "team_total": round(team_total, 2),
            "captain_vs_vice_delta": delta,
            "captain_name": cap_row["name"] if cap_row else None,
            "vice_name": vice_row["name"] if vice_row else None,
            "text": headline_text,
        },
        "rows": starters,
        "bench": bench,
        "espn_matches": espn_matches or [],
        "picks_source": picks_source,
        "masks": masks,
        "data_age_seconds": round(data_age, 1),
        "stale_snapshot": stale_snapshot,
        "graded_now": graded_now,
        "pings_sent": pings_sent,
        "track_record_url": "/track-record",
        "note": " · ".join(note_parts),
        "as_of": now_utc.isoformat(),
    }


def _emit_matchday_pings(
    db: Any,
    gameweek: int,
    session_id: str,
    events: list[dict[str, Any]],
    name_map: dict[int, str],
    captain_pid: int | None,
) -> int:
    """Persist unseen events (per-event dedupe) and dispatch push/bell."""
    try:
        db.execute(text(_LIVE_EVENT_DDL))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — sqlite tests pre-create it
        db.rollback()
        logger.debug("live_event_log DDL skipped: %s", exc)

    from fpl_intelligence.notifications.webpush import dispatch

    try:
        seen_rows = db.execute(
            text(
                "SELECT element_id, event_kind, ordinal FROM live_event_log "
                "WHERE gameweek = :gw"
            ),
            {"gw": int(gameweek)},
        ).all()
    except Exception:  # noqa: BLE001 — table may not exist yet on first run
        db.rollback()
        seen_rows = []
    seen = {(int(r[0]), str(r[1]), int(r[2])) for r in seen_rows}
    sent = 0
    for event in events:
        key = (int(event["element_id"]), str(event["kind"]), int(event["ordinal"]))
        if key in seen:
            continue
        db.execute(
            text(
                "INSERT INTO live_event_log "
                "(gameweek, element_id, event_kind, ordinal, notified_at) "
                "VALUES (:gw, :eid, :kind, :ordinal, :at)"
            ),
            {
                "gw": int(gameweek),
                "eid": key[0],
                "kind": key[1],
                "ordinal": key[2],
                "at": datetime.now(UTC),
            },
        )
        message = event_message(event, name_map, captain_id=captain_pid)
        try:
            dispatch(
                db,
                session_id=str(session_id),
                kind="goals",
                title="Matchday ping",
                body=message,
                url="/live",
            )
        except Exception as exc:  # noqa: BLE001 — bell log already written upstream
            logger.debug("ping dispatch failed: %s", exc)
        sent += 1
    return sent


def _snapshot_payload(
    starters: list[dict[str, Any]],
    bench: list[dict[str, Any]],
    headline_text: str,
    team_total: float,
    espn_matches: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    return {
        "rows_all": starters,
        "bench": bench,
        "headline_text": headline_text,
        "team_total": team_total,
        "espn_matches": espn_matches or [],
    }


def rows_have_data(rows: list[dict[str, Any]]) -> bool:
    """A usable assembly needs at least one row carrying a points value."""
    return any(r.get("points") is not None for r in rows)


def _load_saved_squad(db: Any, session_id: str) -> Any | None:
    """Locally saved squad fallback when FPL picks are unreachable."""
    try:
        from fpl_intelligence.squad.service import SquadService

        return SquadService(session=db).get_squad(session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("saved-squad fallback failed: %s", exc)
        return None
