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
import threading
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
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
    next_gameweeks,
    parse_fixtures,
    player_run,
)
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider
from fpl_intelligence.squad.bridge import DecisionOptimizerBridge
from fpl_intelligence.squad.service import SquadService
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
        p = players.get(str(pid)) or {}
        if p.get("web_name"):
            return str(p["web_name"])
    return f"Player {pid}"


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

    # --- fixture context ------------------------------------------------------
    fixture_lines: list[str] = []
    squad_swing = 0.0
    targets: list[str] = []
    try:
        rows = parse_fixtures(await load_fixtures(db))
        current_gw = max(infer_current_gameweek(rows), squad.gameweek)
        horizon5 = next_gameweeks(rows, current_gw, 5)
        horizon4 = next_gameweeks(rows, current_gw, 4)
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
    try:
        tr = track_record_payload(db, session_key=session_id)
        rolling = tr.get("rolling") or {}
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

    captain = report_dict.get("captain") or {}
    transfers = report_dict.get("transfer_plan") or {}
    chain = (report_dict.get("meta") or {}).get("chain") or {}

    return {
        "gameweek": report.gameweek,
        "session_id": session_id,
        "player_ids": list(squad.player_ids),
        "entry_size": len(squad.player_ids),
        "bank": float(squad.bank),
        "free_transfers": squad.free_transfers,
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
    }


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

    return {
        "squad_status": (
            f"{facts['entry_size']} players loaded · bank £{facts['bank']:.1f}m · "
            f"{facts['free_transfers']} free transfer(s). "
            f"Predictions from {facts['prediction_source']}."
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
        "last_week_grade": facts["grade_line"],
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
) -> dict[str, Any]:
    """Six-section weekly brief: LLM-written when possible, template otherwise."""
    if not session_id:
        raise HTTPException(status_code=404, detail="No squad saved for this session")
    response.headers["Cache-Control"] = "no-store"

    facts = await _gather_facts(db, session_id)
    gameweek = gw or int(facts["gameweek"])
    key = _cache_key(session_id, gameweek, list(facts.get("player_ids") or []))

    cached_hit: dict[str, Any] | None = None
    with _brief_lock:
        cached_hit = _brief_cache.get(key)
    if cached_hit is not None and cached_hit.get("gameweek") == gameweek:
        out = dict(cached_hit)
        out["cached"] = True
        return out

    template_sections = _template_sections({**facts, "gameweek": gameweek})
    sections = template_sections
    model_label = "template-fallback"

    llm = _build_real_provider()
    if not isinstance(llm, MockLLMProvider):
        try:
            from fpl_intelligence.live_intelligence.prompts import LLMPrompt  # noqa: PLC0415

            prompt = LLMPrompt(
                template_id="assistant.brief",
                version="1",
                schema_version="1",
                system=(
                    "You are an FPL weekly assistant. Using ONLY the facts given, "
                    "write a short brief. Reply with STRICT JSON, no code fences, "
                    "exactly the six requested keys. Never invent players, prices "
                    "or statistics absent from the facts."
                ),
                user=_render_facts_text(facts),
            )
            raw = await run_in_threadpool(llm.complete, prompt)
            parsed = _parse_sections(raw.text or "") if raw and raw.text else None
            if parsed is not None:
                sections = parsed
                resp_provider = getattr(raw, "provider_name", None) or ""
                resp_model = getattr(raw, "model_name", None) or ""
                model_label = "/".join(x for x in (resp_provider, resp_model) if x) or "llm"
        except Exception as exc:  # noqa: BLE001 — never fail the brief on LLM errors
            logger.warning("Brief LLM failed (%s); template used.", exc)
            model_label = "template-fallback"
            sections = template_sections

    payload = {
        "session_id": session_id,
        "gameweek": gameweek,
        "sections": {SECTION_TITLES[k]: sections[k] for k in SECTION_KEYS},
        "model": model_label,
        "cached": False,
        "generated_at": datetime.now(UTC).isoformat(),
        "facts_digest": {
            "captain": facts["captain"]["name"],
            "squad_swing": facts["squad_swing"],
            "news_matches": _count_real_news(facts["news_lines"]),
        },
    }
    with _brief_lock:
        _brief_cache[key] = payload
    return payload
