"""Phase 20.0 — weekly assistant brief.

``GET /api/v1/assistant/brief?session_id=&gw=`` builds a structured weekly
brief with six fixed sections (SQUAD STATUS / CAPTAIN / TRANSFERS /
FIXTURE SWINGS / NEWS FLAGS / LAST WEEK GRADE). The text comes from a real
LLM chain (GROQ -> OPENROUTER -> GEMINI) whenever any key is configured;
otherwise — or on real failure — a deterministic template fills every section
and the response is labelled ``template-fallback``.

Briefs are cached in-memory per ``(gameweek, squad hash)`` so refreshes are
free and stable within a gameweek.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import UTC, date, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.api import deps
from fpl_intelligence.api.routes.analyst import _build_real_provider
from fpl_intelligence.api.routes.fixtures import _team_names, load_fixtures
from fpl_intelligence.api.routes.news import _cached_items as cached_news_items
from fpl_intelligence.data_providers.bbc_news import (
    NEWS_KEYWORDS,
    match_headlines,
)
from fpl_intelligence.db.models import Player
from fpl_intelligence.fixtures.scanner import (
    average_fdr,
    easiest_team_runs,
    infer_current_gameweek,
    next_unplayed_gameweeks,
    parse_fixtures,
    player_run,
)
from fpl_intelligence.live_intelligence.llm_audit import audit_providers
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider
from fpl_intelligence.notifications.telegram_bot import get_allowed_user_ids
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
from fpl_intelligence.squad.service import SquadService
from fpl_intelligence.sync.gameweek_clock import resolve_target_gameweek
from fpl_intelligence.sync.materialized_models import AssistantBriefDB
from fpl_intelligence.sync.models import RecommendationDB, SyncLogDB
from fpl_intelligence.sync.service import track_record_payload

router = APIRouter(prefix="/assistant", tags=["assistant"])
logger = logging.getLogger(__name__)

GetDB = deps.GetDB

SECTION_KEYS = (
    "squad_status",
    "captain",
    "transfers",
    "fixture_swings",
    "news_flags",
    "last_week_grade",
)
SECTION_TITLES = {
    "squad_status": "SQUAD STATUS",
    "captain": "CAPTAIN",
    "transfers": "TRANSFERS",
    "fixture_swings": "FIXTURE SWINGS",
    "news_flags": "NEWS FLAGS",
    "last_week_grade": "LAST WEEK GRADE",
}

_brief_cache: dict[str, dict[str, Any]] = {}
_brief_lock = threading.Lock()

_LLM_TIMEOUT_SECONDS = 12.0


def _cache_key(session_id: str, gameweek: int, player_ids: list[int]) -> str:
    payload = json.dumps([session_id, gameweek, sorted(player_ids)], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _name_of(report: dict[str, Any], pid: int | None) -> str:
    players = report.get("players") or {}
    if pid is not None:
        # model_dump() keeps dict[int, ...] keys as ints while the JSON wire
        # format uses strings — accept both.
        p = players.get(str(pid)) or players.get(int(pid)) or {}
        if p.get("web_name"):
            return str(p["web_name"])
        return f"#{pid}"
    return "N/A"


def _radar_name_rows(db: Session, pids: list[int]) -> list[tuple[int, str, str, str]]:
    """(player_id, web_name, first_name, second_name) rows for news matching."""
    from sqlalchemy import select  # noqa: PLC0415

    from fpl_intelligence.db.models import Player  # noqa: PLC0415

    rows: list[tuple[int, str, str, str]] = []
    for pid in set(pids):
        prow: Player | None = db.scalar(select(Player).where(Player.fpl_element_id == pid))
        if prow is None:
            prow = db.get(Player, pid)
        if prow is not None:
            rows.append((pid, prow.web_name, prow.first_name, prow.second_name))
    return rows


# --------------------------------------------------------------------------- #
# Fact gathering
# --------------------------------------------------------------------------- #

async def _gather_facts(db: Session, session_id: str) -> dict[str, Any]:
    """Everything the brief is built from — all real data, no placeholders."""
    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(status_code=404, detail="No squad saved for this session")

    bridge = DecisionOptimizerBridge(provider=deps.get_prediction_provider(db))
    report = bridge.generate_decisions(squad)
    report_dict = report.model_dump()

    # The /decisions route enriches report.players with web names via
    # _build_player_details; the brief calls the bridge directly, so fill
    # minimal name entries straight from the ingested player table.
    if not report_dict.get("players"):
        from sqlalchemy import select  # noqa: PLC0415

        from fpl_intelligence.db.models import Player  # noqa: PLC0415

        name_map: dict[str, dict[str, Any]] = {}
        for pid in squad.player_ids:
            prow: Player | None = db.scalar(
                select(Player).where(Player.fpl_element_id == int(pid))
            )
            name_map[str(pid)] = {"web_name": prow.web_name if prow else f"Player {pid}"}
        report_dict["players"] = name_map

    # --- fixture context ------------------------------------------------------
    fixture_lines: list[str] = []
    squad_swing = 0.0
    targets: list[str] = []
    try:
        rows = parse_fixtures(await load_fixtures(db))
        current_gw = max(
            infer_current_gameweek(rows),
            await resolve_target_gameweek(db, fallback=int(squad.gameweek)),
            int(squad.gameweek),
        )
        horizon5 = next_unplayed_gameweeks(rows, current_gw, 5)
        horizon4 = next_unplayed_gameweeks(rows, current_gw, 4)
        team_names = _team_names(db)
        rows_by_gw: dict[int, list[Any]] = {}
        for r in rows:
            rows_by_gw.setdefault(r.event, []).append(r)
        starter_avgs: list[float] = []
        for idx, pid in enumerate(squad.player_ids):
            team = (squad.player_teams or {}).get(pid)
            runs = [r for r in player_run(team, rows_by_gw, horizon5, team_names=team_names)]
            real = [r for r in runs if r.opponent_id != 0]
            avg = round(average_fdr(real), 2) if real else 3.0
            if idx < 11:
                starter_avgs.append(avg)
                name = _name_of(report_dict, pid)
                run_txt = ", ".join(
                    f"{r.opponent}{'(H)' if r.is_home else '(A)'}{r.difficulty}"
                    for r in runs[:3]
                )
                fixture_lines.append(f"{name}: {run_txt}")
        squad_swing = round(sum(3.0 - a for a in starter_avgs), 2)
        exclude = {t for t in (squad.player_teams or {}).values() if t}
        targets = [
            f"{t.short_name} (avg FDR {t.avg_fdr})"
            for t in easiest_team_runs(
                rows_by_gw, horizon4, top=5, exclude_teams=exclude, team_names=team_names
            )
        ]
    except Exception as exc:  # noqa: BLE001 — fixtures enrich, never block the brief
        logger.warning("brief fixture scan failed: %s", exc)

    # --- news flags -----------------------------------------------------------
    news_lines: list[str] = []
    try:
        items = await cached_news_items()
        if items:
            name_rows = _radar_name_rows(db, list(squad.player_ids))
            flags = match_headlines(items, name_rows, NEWS_KEYWORDS)
            by_pid = {pid: web for pid, web, _, _ in name_rows}
            for pid_str, flag in flags.items():
                pid = int(pid_str)
                kw = flag["keywords"][0] if flag["keywords"] else "news"
                news_lines.append(f"{by_pid.get(pid, pid)}: {flag['headline']} ({kw})")
            if not flags:
                news_lines.append("No BBC headlines matched your squad.")
        else:
            news_lines.append("BBC feed unavailable right now — no news check this cycle.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("brief news scan failed: %s", exc)
        news_lines.append("News radar failed this cycle.")

    # --- track record ---------------------------------------------------------
    grade_line = "No graded weeks yet — the ledger fills after your first gameweek."
    track_rolling: dict[str, Any] = {}
    try:
        tr = track_record_payload(db, session_key=session_id)
        rolling = tr.get("rolling") or {}
        track_rolling = rolling
        if rolling.get("graded"):
            hit_rate = rolling.get("hit_rate")
            net = rolling.get("net_points")
            last = (rolling.get("last_5") or [None])[0]
            last_txt = ""
            if isinstance(last, dict):
                score = last.get("score") or {}
                verdict = score.get("verdict", "?")
                delta = score.get("delta", 0)
                last_txt = f"; latest call was {verdict} by {delta:+d} pts"
            hr = f"{hit_rate * 100:.0f}%" if isinstance(hit_rate, (int, float)) else "–"
            grade_line = (
                f"{rolling.get('graded')} graded calls · {hr} hits · "
                f"net {net:+d} pts{last_txt}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("brief track record failed: %s", exc)

    # --- Phase 23 (L1/L3): league edge + price moves ---------------------------
    league_edge_lines = _league_edge_lines(db, session_id)
    price_note = _price_moves_note(db)

    captain = report_dict.get("captain") or {}
    transfers = report_dict.get("transfer_plan") or {}
    chain = (report_dict.get("meta") or {}).get("chain") or {}
    chip = report_dict.get("chip") or {}

    return {
        "gameweek": report.gameweek,
        "session_id": session_id,
        "player_ids": list(squad.player_ids),
        "entry_size": len(squad.player_ids),
        "bank": float(squad.bank),
        "free_transfers": squad.free_transfers,
        "entry_label": _resolve_entry_label(db, session_id),
        "squad_names": _squad_names(db, list(squad.player_ids)),
        "chip_name": chip.get("chip_name"),
        "chip_reason": chip.get("main_reason", ""),
        "track_rolling": track_rolling,
        "captain": {
            "name": _name_of(report_dict, captain.get("player_id")),
            "xpts": captain.get("expected_points"),
            "alternatives": [
                {
                    "name": _name_of(report_dict, alt.get("player_id")),
                    "xpts": alt.get("expected_points"),
                }
                for alt in (captain.get("alternatives") or [])[:2]
            ],
        },
        "transfer_action": transfers.get("action_type", "roll"),
        "transfer_reason": transfers.get("main_reason", ""),
        "transfer_ins": [_name_of(report_dict, p) for p in (transfers.get("transfers_in") or [])],
        "transfer_outs": [_name_of(report_dict, p) for p in (transfers.get("transfers_out") or [])],
        "prediction_source": chain.get("source_label", "the prediction engine"),
        "fixture_lines": fixture_lines,
        "squad_swing": squad_swing,
        "targets": targets,
        "news_lines": news_lines,
        "grade_line": grade_line,
        "league_edge_lines": league_edge_lines,
        "price_note": price_note,
    }


def _league_edge_lines(db: Session, session_id: str) -> list[str]:
    """Phase 23 (L1): LEAGUE EDGE brief lines from the league cache.

    Honest by construction: when no cached league data exists the section is
    simply absent — never a placeholder.
    """
    try:
        from sqlalchemy import select as _select

        from fpl_intelligence.leagues.models import (
            LeagueCacheDB,
            LeagueSelectionDB,
        )

        sel = db.scalar(
            _select(LeagueSelectionDB).where(
                LeagueSelectionDB.session_id == str(session_id)
            )
        )
        cache_row: LeagueCacheDB | None = None
        if sel is not None:
            cache_row = db.get(LeagueCacheDB, int(sel.league_id))
        if cache_row is None:
            cache_row = db.scalar(_select(LeagueCacheDB).limit(1))
        if cache_row is None or not (cache_row.standings or []):
            return []

        standings = [r for r in cache_row.standings if isinstance(r, dict)]
        mine = next(
            (r for r in standings if str(r.get("entry_id")) == str(session_id)),
            None,
        )
        lines: list[str] = []
        league_label = cache_row.name or f"League {cache_row.league_id}"
        if mine is not None:
            gap_txt = ""
            if len(standings) >= 3 and mine.get("total") is not None \
                    and standings[2].get("total") is not None:
                gap = int(standings[2]["total"]) - int(mine["total"])
                gap_txt = f", {gap:+d} to the top 3"
            lines.append(
                f"{league_label}: rank #{mine.get('rank')} of "
                f"{cache_row.member_count or len(standings)}{gap_txt}"
            )
        rp = cache_row.rivals_picks or {}
        picks_map = {
            k: v for k, v in (rp.get("picks") or {}).items() if isinstance(v, list)
        }
        if picks_map and isinstance(rp.get("captains"), dict):
            captains = {int(k): int(v) for k, v in rp["captains"].items() if v}
            my_cap_row = db.scalar(
                _select(RecommendationDB.subject).where(  # type: ignore[arg-type]
                    RecommendationDB.session_key == str(session_id),
                    RecommendationDB.rec_type == "captain",
                )
            )
            my_captain = (
                int(my_cap_row.get("captain_id") or 0)
                if isinstance(my_cap_row, dict)
                else None
            )
            if my_captain and my_captain in captains.values():
                n = sum(1 for c in captains.values() if c == my_captain)
                lines.append(f"{n} top rival(s) also captain your pick.")
            elif my_captain and captains:
                lines.append("Your captain differential: no top rival captains him.")
    except Exception as exc:  # noqa: BLE001 — enrichment only
        logger.debug("league edge lines failed: %s", exc)
        return []
    return lines[:3]


def _price_moves_note(db: Session) -> str | None:
    """Phase 23 (L3): one-line risers/fallers note for the brief."""
    try:
        from fpl_intelligence.prices.service import todays_moves_payload

        payload = todays_moves_payload(db, limit=3)
        if not payload["has_data"]:
            return None
        parts: list[str] = []
        if payload["risers"]:
            parts.append(
                "Risers: "
                + ", ".join(f"{c['web_name']} ({c['label']})" for c in payload["risers"])
            )
        if payload["fallers"]:
            parts.append(
                "Fallers: "
                + ", ".join(f"{c['web_name']} ({c['label']})" for c in payload["fallers"])
            )
        return " · ".join(parts)
    except Exception as exc:  # noqa: BLE001 — enrichment only
        logger.debug("price note failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Template fallback + LLM rendering
# --------------------------------------------------------------------------- #

def _template_sections(facts: dict[str, Any]) -> dict[str, str]:
    """Deterministic six-section brief when no LLM answered."""
    cap = facts["captain"]
    cap_txt = cap["xpts"]
    cap_xpts = f"{cap_txt:.1f}" if isinstance(cap_txt, (int, float)) else "–"
    alt_txt = "; ".join(
        f"{a['name']} {a['xpts']:.1f}"
        for a in cap["alternatives"]
        if a["xpts"] is not None
    )

    if facts["transfer_action"] == "roll":
        transfer_txt = "Roll the free transfer."
    elif facts["transfer_ins"]:
        ins = ", ".join(facts["transfer_ins"])
        outs = ", ".join(facts["transfer_outs"]) or "bench cover"
        transfer_txt = f"{facts['transfer_action']}: IN {ins}, OUT {outs}."
    else:
        transfer_txt = "Hold the squad."

    swing_word = (
        "easy" if facts["squad_swing"] > 0.5
        else ("hard" if facts["squad_swing"] < -0.5 else "neutral")
    )

    # Phase 20.4 — the fallback must be personal: real entry label, real squad
    # names and real numbers, never a generic wall of text.
    label = facts.get("entry_label") or f"Entry #{facts.get('session_id', '?')}"
    squad_names = [n for n in (facts.get("squad_names") or []) if n]
    xi_names = ", ".join(squad_names[:11]) if squad_names else ""
    last_call = _last_call_line(facts)

    return {
        "squad_status": (
            f"{label}: {facts['entry_size']} players loaded · bank £{facts['bank']:.1f}m · "
            f"{facts['free_transfers']} free transfer(s). "
            + (f"Squad: {xi_names}. " if xi_names else "")
            + f"Predictions from {facts['prediction_source']}."
        ),
        "captain": (
            f"{cap['name']} captains with xPTS {cap_xpts}"
            + (f" ahead of {alt_txt}." if alt_txt else ".")
        ),
        "transfers": transfer_txt,
        "fixture_swings": (
            f"Squad swing {facts['squad_swing']:+.1f} ({swing_word} patch). "
            + ("Easiest upcoming runs: " + ", ".join(facts["targets"]) + "."
               if facts["targets"] else "")
        ).strip(),
        "news_flags": " ".join(facts["news_lines"]) or "No news matches.",
        "last_week_grade": (
            f"{last_call} {facts['grade_line']}" if last_call else facts["grade_line"]
        ),
    }


def _render_facts_text(facts: dict[str, Any]) -> str:
    """Compact fact sheet fed to the LLM prompt."""
    lines = [
        f"Gameweek: {facts['gameweek']}",
        f"Squad: {facts['entry_size']} players, bank £{facts['bank']:.1f}m, "
        f"{facts['free_transfers']} FT",
        f"Prediction source: {facts['prediction_source']}",
        f"Captain candidate: {facts['captain']['name']} (xPTS {facts['captain']['xpts']})",
    ]
    for alt in facts["captain"]["alternatives"]:
        lines.append(f"Alternative: {alt['name']} (xPTS {alt['xpts']})")
    lines.append(
        f"Transfer stance: {facts['transfer_action']} — "
        f"{facts['transfer_reason'] or 'no reason recorded'}"
    )
    if facts["fixture_lines"]:
        lines.append("Fixture swings (next GWs, FDR):")
        lines.extend(f"  {line}" for line in facts["fixture_lines"])
    lines.append(f"Squad swing score: {facts['squad_swing']:+.1f}")
    if facts["targets"]:
        lines.append("Transfer-target team runs: " + ", ".join(facts["targets"]))
    lines.append("News flags:")
    lines.extend(f"  {line}" for line in facts["news_lines"])
    lines.append(f"Track record: {facts['grade_line']}")
    return "\n".join(lines)


def _count_real_news(lines: list[str]) -> int:
    """Headline lines carry 'Name: text'; status lines do not."""
    return sum(
        1
        for ln in lines
        if ": " in ln
        and "No BBC" not in ln
        and "unavailable" not in ln
        and "failed" not in ln
    )


# --------------------------------------------------------------------------- #
# Phase 20.4 — personalization + TL;DR action card
# --------------------------------------------------------------------------- #


def _resolve_entry_label(db: Session, session_id: str) -> str:
    """The manager's team name when a bookmarklet push recorded one, else an
    honest 'Entry #id' label. Never invented."""
    try:
        row = db.execute(
            select(SyncLogDB)
            .where(
                SyncLogDB.kind == "squad",
                SyncLogDB.entry_id == str(session_id),
            )
            .order_by(SyncLogDB.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — cosmetic lookup only
        return f"Entry #{session_id}"
    name = ((row.detail or {}).get("entry_name") if row is not None else None) or ""
    return str(name).strip() or f"Entry #{session_id}"


def _squad_names(db: Session, pids: list[int]) -> list[str]:
    """Real web names for squad ids, in squad order (unknown ids skipped)."""
    names: list[str] = []
    for pid in pids:
        prow: Player | None = db.scalar(select(Player).where(Player.fpl_element_id == int(pid)))
        if prow is not None and prow.web_name:
            names.append(prow.web_name)
    return names


def _last_call_line(facts: dict[str, Any]) -> str | None:
    """'We said X, result Y — right/wrong' from the newest graded card."""
    rolling = facts.get("track_rolling") or {}
    cards = [c for c in rolling.get("last_5", []) if c.get("score")]
    if not cards:
        return None
    newest = max(cards, key=lambda c: (c.get("gameweek") or 0))
    said = str((newest.get("detail") or {}).get("reason") or "").strip()
    score = newest.get("score") or {}
    verdict = str(score.get("verdict") or "?")
    delta = int(score.get("delta") or 0)
    gw = newest.get("gameweek")
    kind = str(newest.get("rec_type") or "call")
    said_txt = said if said else f"our GW{gw} {kind} call"
    right = verdict in ("right", "neutral")
    return (
        f"We said: {said_txt}. Result: {verdict} by {delta:+d} pts — "
        f"the GW{gw} {kind} call was {'right' if right else 'wrong'}."
    )


def _confidence_captain(cap_xpts: Any, alt_xpts: Any) -> int:
    if isinstance(cap_xpts, (int, float)) and isinstance(alt_xpts, (int, float)):
        margin = abs(float(cap_xpts) - float(alt_xpts))
        return min(92, 55 + int(margin * 10))
    return 70


def _tldr_actions(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Exactly three actions: CAPTAIN / TRANSFERS / CHIP, each with confidence %.

    Confidence is a transparent heuristic over the same numbers shown in the
    sections (xPTS margins and fixture-swing size) — never a black box.
    """
    cap = facts["captain"]
    alts = cap.get("alternatives") or []

    # --- Action 1: captain -----------------------------------------------------
    if alts and alts[0].get("name"):
        margin_raw = None
        if isinstance(cap.get("xpts"), (int, float)) and isinstance(
            alts[0].get("xpts"), (int, float)
        ):
            margin_raw = round(float(cap["xpts"]) - float(alts[0]["xpts"]), 1)
        reason_bits = ["highest projected return in your XI"]
        if facts.get("fixture_lines"):
            reason_bits.append(f"fixtures: {facts['fixture_lines'][0]}")
        first_flag = next((ln for ln in facts["news_lines"] if ": " in ln), None)
        if first_flag:
            reason_bits.append(f"news: {first_flag}")
        action1 = {
            "kind": "CAPTAIN",
            "text": (
                f"CAPTAIN {cap['name']} over {alts[0]['name']} "
                + (f"by +{margin_raw} xPTS" if margin_raw is not None else "(projection gap)")
            ),
            "reason": "; ".join(reason_bits),
            "confidence": _confidence_captain(cap.get("xpts"), alts[0].get("xpts")),
        }
    else:
        action1 = {
            "kind": "CAPTAIN",
            "text": f"CAPTAIN {cap['name']}",
            "reason": "no viable alternative projected higher",
            "confidence": 70,
        }

    # --- Action 2: transfers ---------------------------------------------------
    swing = float(facts.get("squad_swing") or 0.0)
    swing_word = (
        "easy fixture patch ahead"
        if swing > 0.5
        else ("hard fixture patch ahead" if swing < -0.5 else "neutral fixtures")
    )
    if facts["transfer_action"] == "roll":
        action2 = {
            "kind": "TRANSFERS",
            "text": f"TRANSFERS: roll ({facts['free_transfers']} FT banked)",
            "reason": facts.get("transfer_reason") or f"{swing_word}; no upgrade clears the bar",
            "confidence": min(88, 60 + int(abs(swing) * 8)),
        }
    elif facts.get("transfer_ins"):
        ins = ", ".join(facts["transfer_ins"])
        outs = ", ".join(facts["transfer_outs"]) or "bench cover"
        action2 = {
            "kind": "TRANSFERS",
            "text": f"TRANSFERS: IN {ins} · OUT {outs}",
            "reason": facts.get("transfer_reason") or swing_word,
            "confidence": min(90, 62 + int(abs(swing) * 6)),
        }
    else:
        action2 = {
            "kind": "TRANSFERS",
            "text": "TRANSFERS: hold squad",
            "reason": facts.get("transfer_reason") or swing_word,
            "confidence": 65,
        }

    # --- Action 3: chips ---------------------------------------------------------
    chip_name = facts.get("chip_name")
    chip_reason = facts.get("chip_reason") or ""
    if chip_name:
        action3 = {
            "kind": "CHIP",
            "text": f"CHIP: use {chip_name.upper()} this week",
            "reason": chip_reason or "engine flags this week as the best chip window",
            "confidence": 68,
        }
    else:
        action3 = {
            "kind": "CHIP",
            "text": "CHIP: save all chips this week",
            "reason": chip_reason
            or "no double gameweek or blank week detected within the horizon",
            "confidence": 72,
        }

    return [action1, action2, action3]



def _parse_sections(raw_text: str) -> dict[str, str] | None:
    """Strict-JSON section parse; tolerant of fenced envelopes."""
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").lstrip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].lstrip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    sections = {k: str(parsed[k]).strip() for k in SECTION_KEYS if parsed.get(k)}
    return sections if len(sections) == len(SECTION_KEYS) else None


@router.get("/brief")
async def assistant_brief(
    response: Response,
    db: GetDB,
    session_id: str | None = Query(None),
    gw: int | None = Query(None, description="Optional gameweek override."),
    generate: bool = Query(
        False,
        description="Allow inline LLM generation. Only the daily cron sets this; "
        "request paths always read the pre-generated brief or fall back to the "
        "personal template instantly.",
    ),
) -> dict[str, Any]:
    """Six-section weekly brief — READ-ONLY for requests, generated by the cron.

    Phase 21.1 (T3): request paths never run an LLM. Order of operations:

    1. in-memory cache hit -> return immediately;
    2. persisted ``assistant_briefs`` row for this (session, GW) -> return it
       (survives serverless cold starts);
    3. miss + ``generate=False`` -> personal template sections built from real
       facts, stored, returned (sub-second, never a spinner);
    4. miss + ``generate=True`` (daily 06:10 cron only) -> LLM chain with the
       pinned ``assistant.brief`` prompt, then persist.
    """
    if not session_id:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    response.headers["Cache-Control"] = "no-store"

    squad = SquadService(session=db).get_squad(session_id=session_id)
    if squad is None:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    gameweek = gw or await resolve_target_gameweek(db, fallback=int(squad.gameweek))

    # --- fast paths: memory, then the durable table ---------------------------
    key = _cache_key(session_id, gameweek, list(squad.player_ids))
    with _brief_lock:
        cached_hit = _brief_cache.get(key)
    if cached_hit is not None and cached_hit.get("gameweek") == gameweek:
        out = dict(cached_hit)
        out["cached"] = True
        out["source"] = "memory"
        return out

    _ensure_brief_table(db)
    stored = _load_brief_row(db, session_id, gameweek)
    if stored is not None and not generate:
        out = dict(stored)
        out["cached"] = True
        out["source"] = "database"
        with _brief_lock:
            _brief_cache[key] = dict(out)
        return out

    facts = await _gather_facts(db, session_id)
    facts["gameweek"] = gameweek
    template_sections = _template_sections(facts)
    sections = template_sections
    model_label = "template-fallback"

    llm_audit_rows: list[dict[str, Any]] = []
    if generate:
        # Cron-only path: prod LLM audit (which models serve RIGHT NOW) plus
        # the pinned brief template. Request paths skip all of this.
        try:
            llm_audit_rows = await audit_providers()
        except Exception as exc:  # noqa: BLE001 — audit never blocks the brief
            logger.warning("llm audit failed: %s", exc)

        llm = _build_real_provider()
        if not isinstance(llm, MockLLMProvider):
            try:
                from fpl_intelligence.live_intelligence.prompts import (  # noqa: PLC0415
                    ASSISTANT_BRIEF,
                    LLMPrompt,
                )

                prompt = LLMPrompt(
                    template_id=ASSISTANT_BRIEF.template_id,
                    version=ASSISTANT_BRIEF.version,
                    schema_version=ASSISTANT_BRIEF.schema_version,
                    system=ASSISTANT_BRIEF.system,
                    user=_render_facts_text(facts),
                )
                raw = await run_in_threadpool(llm.complete, prompt)
                parsed = _parse_sections(raw.text or "") if raw and raw.text else None
                if parsed is not None:
                    sections = parsed
                    resp_provider = getattr(raw, "provider_name", None) or ""
                    resp_model = getattr(raw, "model_name", None) or ""
                    model_label = (
                        "/".join(x for x in (resp_provider, resp_model) if x) or "llm"
                    )
                else:
                    logger.warning(
                        "Brief LLM reply not parseable as six-section JSON; template used."
                    )
            except Exception as exc:  # noqa: BLE001 — never fail the brief on LLM errors
                logger.warning("Brief LLM failed (%s); template used.", exc)
                model_label = "template-fallback"
                sections = template_sections

    payload = {
        "session_id": session_id,
        "gameweek": gameweek,
        "sections": {SECTION_TITLES[k]: sections[k] for k in SECTION_KEYS},
        "extra_sections": _extra_sections(facts),
        "model": model_label,
        "cached": False,
        "source": "generated" if generate else "template",
        "generated_at": datetime.now(UTC).isoformat(),
        "tldr": _tldr_actions(facts),
        "llm_audit": llm_audit_rows,
        "facts_digest": {
            "captain": facts["captain"]["name"],
            "squad_swing": facts["squad_swing"],
            "news_matches": _count_real_news(facts["news_lines"]),
            "entry_label": facts.get("entry_label"),
            "squad_names_count": len(facts.get("squad_names") or []),
        },
    }
    with _brief_lock:
        _brief_cache[key] = dict(payload)
    _store_brief(db, session_id, gameweek, payload)

    if generate:
        # Phase 23 (L2): cron-only brief push through the self-hosted channel
        # (bell always logs it; browser push only when the trigger is on).
        try:
            from fpl_intelligence.notifications.webpush import (
                dispatch as webpush_dispatch,
            )

            first_action = (payload["tldr"] or [{}])[0].get("text", "")
            webpush_dispatch(
                db,
                session_id=str(session_id),
                kind="brief",
                title=f"Weekly brief · GW{gameweek}",
                body=first_action or "Your weekly brief is ready.",
                url="/assistant",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort push
            logger.debug("brief webpush failed: %s", exc)

    payload["telegram"] = await maybe_push_brief(db, session_id, gameweek, payload)
    return payload


def _extra_sections(facts: dict[str, Any]) -> dict[str, str]:
    """Phase 23 additive sections (LEAGUE EDGE / PRICE MOVES) — never placeholders."""
    extras: dict[str, str] = {}
    lines = facts.get("league_edge_lines") or []
    if lines:
        extras["LEAGUE EDGE"] = " ".join(lines)
    price_note = facts.get("price_note")
    if price_note:
        extras["PRICE MOVES"] = str(price_note)
    return extras


# --------------------------------------------------------------------------- #
# Phase 21.1 — durable brief storage (survives serverless cold starts)
# --------------------------------------------------------------------------- #

_BRIEF_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS assistant_briefs (
        id SERIAL PRIMARY KEY,
        session_id VARCHAR(255) NOT NULL,
        gameweek INTEGER NOT NULL,
        model VARCHAR(120),
        payload JSON NOT NULL DEFAULT '{}'::json,
        generated_at TIMESTAMP WITH TIME ZONE NOT NULL
    )
    """,
)


def _ensure_brief_table(db: Session) -> None:
    """Idempotent DDL so old deployments work before alembic catches up."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    try:
        if sa_inspect(db.get_bind()).has_table("assistant_briefs"):
            return
        for ddl in _BRIEF_TABLE_DDL:
            db.execute(text(ddl))
        db.commit()
    except Exception as exc:  # noqa: BLE001 - sqlite tests pre-create via metadata
        db.rollback()
        logger.debug("assistant_briefs DDL skipped: %s", exc)


def _load_brief_row(db: Session, session_id: str, gameweek: int) -> dict[str, Any] | None:
    """Persisted payload for (session, gw), or ``None``."""
    try:
        row = db.scalar(
            select(AssistantBriefDB).where(
                AssistantBriefDB.session_id == str(session_id),
                AssistantBriefDB.gameweek == int(gameweek),
            )
        )
    except Exception as exc:  # noqa: BLE001 — table may not exist yet
        db.rollback()
        logger.debug("brief row read failed: %s", exc)
        return None
    if row is None or not isinstance(row.payload, dict):
        return None
    out = dict(row.payload)
    out.setdefault("gameweek", int(gameweek))
    out.setdefault("session_id", str(session_id))
    if row.model and not out.get("model"):
        out["model"] = row.model
    return out


def load_pregenerated_brief(
    db: Session, session_id: str, gameweek: int | None = None
) -> dict[str, Any] | None:
    """Public read helper for sibling routes (analyst summary).

    Resolves the target gameweek synchronously when not supplied: prefers an
    exact stored row at any recent gameweek over a network call.
    """
    _ensure_brief_table(db)
    rows = db.execute(
        select(AssistantBriefDB)
        .where(AssistantBriefDB.session_id == str(session_id))
        .order_by(AssistantBriefDB.gameweek.desc(), AssistantBriefDB.generated_at.desc())
        .limit(5)
    ).scalars().all()
    if not rows:
        return None
    if gameweek is not None:
        for row in rows:
            if int(row.gameweek) == int(gameweek) and isinstance(row.payload, dict):
                return dict(row.payload)
    newest = rows[0]
    return dict(newest.payload) if isinstance(newest.payload, dict) else None


def _store_brief(
    db: Session, session_id: str, gameweek: int, payload: dict[str, Any]
) -> None:
    """Upsert the durable copy; failures are logged, never raised."""
    try:
        row = db.scalar(
            select(AssistantBriefDB).where(
                AssistantBriefDB.session_id == str(session_id),
                AssistantBriefDB.gameweek == int(gameweek),
            )
        )
        now = datetime.now(UTC)
        if row is None:
            row = AssistantBriefDB(
                session_id=str(session_id),
                gameweek=int(gameweek),
                model=payload.get("model"),
                payload=dict(payload),
                generated_at=now,
            )
            db.add(row)
        else:
            row.model = payload.get("model")
            row.payload = dict(payload)
            row.generated_at = now
        db.commit()
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        db.rollback()
        logger.warning("brief persistence failed for %s gw%s: %s", session_id, gameweek, exc)


# --------------------------------------------------------------------------- #
# Phase 20.4 — one-shot Telegram push for fresh briefs from Friday onward
# --------------------------------------------------------------------------- #

_PUSH_MARKER_PREFIX = "assistant-push"


def _push_marker_name(session_id: str, gameweek: int) -> str:
    return f"{_PUSH_MARKER_PREFIX}-{session_id}-{gameweek}"


def _already_pushed(db: Session, session_id: str, gameweek: int) -> bool:
    from fpl_intelligence.db.models import IngestionRun

    row = db.scalar(
        select(IngestionRun).where(
            IngestionRun.job_name == _push_marker_name(session_id, gameweek),
            IngestionRun.status == "SUCCESS",
        )
    )
    return row is not None


def _record_push(db: Session, session_id: str, gameweek: int, ok: bool) -> None:
    from fpl_intelligence.db.models import IngestionRun

    now = datetime.now(UTC)
    db.add(
        IngestionRun(
            source="assistant-push",
            job_name=_push_marker_name(session_id, gameweek),
            season_code="2026-27",
            status="SUCCESS" if ok else "FAILED",
            started_at=now,
            finished_at=now,
            records_processed=0,
        )
    )
    db.commit()


async def maybe_push_brief(
    db: Session, session_id: str, gameweek: int, brief: dict[str, Any]
) -> dict[str, Any]:
    """First generation on/after Friday attempts a Telegram push (once per GW).

    Returns an honest {configured, attempted, pushed} block either way so the
    UI can say exactly what happened with notifications.
    """
    info = {"configured": False, "attempted": False, "pushed": False}
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = get_allowed_user_ids()
    info["configured"] = bool(token and chat_ids)

    # Friday=4 .. Sunday=6 (UTC): the deadline weekend window.
    if date.today().weekday() < 4:
        return info
    if not info["configured"]:
        info["attempted"] = False
        return info
    if _already_pushed(db, session_id, gameweek):
        info["attempted"] = False
        return info

    entry_name = ((brief.get("facts_digest") or {}).get("captain")) or "squad"
    text = format_brief_message(brief, str(entry_name))
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            pushed_any = False
            for chat_id in chat_ids:
                r = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                )
                r.raise_for_status()
                pushed_any = True
    except Exception as exc:  # noqa: BLE001 — push failure must not fail the brief
        logger.warning("assistant brief push failed: %s", exc)
        info["attempted"] = True
        _record_push(db, session_id, gameweek, ok=False)
        return info
    info["attempted"] = True
    info["pushed"] = pushed_any
    if pushed_any:
        _record_push(db, session_id, gameweek, ok=True)
    return info


def format_brief_message(brief: dict[str, Any], entry_name: str | None) -> str:
    """Telegram HTML rendering of the brief incl. the TL;DR card."""
    sections = brief.get("sections") or {}
    lines = [f"<b>Weekly Brief · GW{brief.get('gameweek', '?')}</b>"]
    if entry_name:
        lines[0] += f" — {entry_name}"
    for act in brief.get("tldr") or []:
        lines.append(
            f"• <b>{act['kind']}</b>: {act['text']} ({act.get('confidence', '?')}%)"
        )
    for title, body in sections.items():
        lines.append(f"\n<b>{title}</b>\n{body}")
    for title, body in (brief.get("extra_sections") or {}).items():
        lines.append(f"\n<b>{title}</b>\n{body}")
    lines.append(f"\n<i>answering model: {brief.get('model', 'unknown')}</i>")
    return "\n".join(lines)
