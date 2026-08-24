"""Phase 19.0 — DB-backed sync services.

Bridges the push endpoints to persistence and keeps the derived math current:

* :func:`ingest_history_gameweek` — store vaastav-format results, mirror them
  into ``PlayerGameweekPerformance`` so the Level-2 baseline rebuilds on the
  new form, then auto-score pending recommendations and reconcile the
  prediction ledger for that gameweek.
* :func:`capture_pre_ingest_predictions` — snapshot what the model WOULD have
  predicted for a gameweek using strictly-earlier data (leakage-safe) before
  the actuals land.
* :func:`record_recommendations` — persist XI/captain/transfer/chip calls made
  by every /api/v1/decisions response so they can be graded later.

All functions are idempotent: re-pushing the same gameweek upserts rather than
duplicating.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import (
    Gameweek,
    Player,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
    Season,
)
from fpl_intelligence.sync.models import (
    IngestedGameweekDB,
    PredictionLedgerDB,
    RecommendationDB,
    SyncLivePointDB,
)
from fpl_intelligence.sync.scoring import (
    NEUTRAL,
    compute_calibration,
    score_captain,
    score_transfer,
    score_xi,
)

logger = logging.getLogger(__name__)

#: Season code used when creating missing Gameweek rows for mirrored history.
DEFAULT_SEASON_CODE = "2026-27"


def _now() -> datetime:
    return datetime.now(UTC)


def _get_or_create_season(db: Session, code: str = DEFAULT_SEASON_CODE) -> Season:
    season = db.scalar(select(Season).where(Season.code == code))
    if season is None:
        season = Season(code=code, display_name=code.replace("-", "/"))
        db.add(season)
        db.flush()
    return season


def _get_or_create_gameweek(db: Session, gameweek: int) -> Gameweek:
    row = db.scalar(select(Gameweek).where(Gameweek.provider_event_id == gameweek))
    if row is None:
        row = Gameweek(
            season_id=_get_or_create_season(db).id,
            provider_event_id=gameweek,
            name=f"Gameweek {gameweek}",
            status="completed",
        )
        db.add(row)
        db.flush()
    return row


def _latest_team_for_player(db: Session, player_id: int) -> int | None:
    return db.execute(
        select(PlayerTeamMembership.team_id)
        .where(PlayerTeamMembership.player_id == player_id)
        .order_by(PlayerTeamMembership.valid_from.desc().nulls_last())
        .limit(1)
    ).scalar_one_or_none()


def capture_pre_ingest_predictions(
    db: Session,
    gameweek: int,
    *,
    source: str = "baseline-model",
) -> int:
    """Snapshot leakage-safe baseline xPTS for ``gameweek`` into the ledger.

    Uses the Level-2 baseline math itself, which only ever reads gameweeks
    strictly BEFORE the target. Because this runs BEFORE the gameweek's own
    rows are inserted, the snapshot is exactly the forecast the engine would
    have published before kickoff. Existing ledger rows are kept (first write
    wins) so repeated pushes never overwrite a genuine pre-match forecast.
    """
    from fpl_intelligence.db.models import Player
    from fpl_intelligence.prediction.live_provider import _baseline_points_for_gameweek

    level = _baseline_points_for_gameweek(db, gameweek)
    if not level or not level.points:
        logger.info("pre-ingest capture skipped: no baseline coverage for gw%s", gameweek)
        return 0
    # The baseline level keys by internal player id; the ledger and all pushed
    # actuals use official FPL element ids. Translate, skipping unmapped rows
    # (they could never be reconciled anyway).
    id_map = {
        int(row[0]): int(row[1])
        for row in db.execute(select(Player.id, Player.fpl_element_id)).all()
        if row[0] is not None and row[1] is not None
    }
    stored = 0
    existing = {
        int(r[0])
        for r in db.execute(
            select(PredictionLedgerDB.element_id).where(PredictionLedgerDB.gameweek == gameweek)
        ).all()
    }
    for player_id, predicted in level.points.items():
        element_id = id_map.get(int(player_id))
        if element_id is None or element_id in existing:
            continue
        db.add(
            PredictionLedgerDB(
                gameweek=gameweek,
                element_id=element_id,
                predicted=float(predicted),
                source=source,
                created_at=_now(),
            )
        )
        stored += 1
    db.flush()
    return stored


def ingest_history_gameweek(
    db: Session,
    gameweek: int,
    elements: list[dict[str, Any]],
    *,
    source: str = "github-actions",
) -> dict[str, Any]:
    """Ingest finalised per-element results for one gameweek.

    Steps (all idempotent):
      1. capture pre-ingest predictions for this gameweek (before writing),
      2. upsert ``ingested_history`` rows,
      3. mirror into ``PlayerGameweekPerformance`` (resolving element ids via
         ``players.fpl_element_id``) so Level-2 form features rebuild,
      4. fill ledger actuals + compute calibration,
      5. score pending recommendations for <= this gameweek.
    """
    stored = mirrored = ledger_filled = 0
    gw_row = _get_or_create_gameweek(db, gameweek)

    # 1 — capture BEFORE the new rows change the feature window.
    try:
        captured = capture_pre_ingest_predictions(db, gameweek)
    except Exception as exc:  # noqa: BLE001 — capture must never block ingestion
        logger.warning("pre-ingest capture failed for gw%s: %s", gameweek, exc)
        captured = 0

    # 2/3 — upsert rows + mirror where the player is resolvable.
    players_by_element = {
        int(row[0]): int(row[1])
        for row in db.execute(select(Player.fpl_element_id, Player.id)).all()
        if row[0] is not None
    }
    for el in elements:
        try:
            element_id = int(el["element_id"])
            total_points = int(el.get("total_points") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        payload = {
            k: v
            for k, v in el.items()
            if k not in ("element_id",) and v is not None
        }
        existing = db.scalar(
            select(IngestedGameweekDB).where(
                IngestedGameweekDB.gameweek == gameweek,
                IngestedGameweekDB.element_id == element_id,
            )
        )
        if existing is None:
            db.add(
                IngestedGameweekDB(
                    gameweek=gameweek,
                    element_id=element_id,
                    source=source,
                    total_points=total_points,
                    minutes=_opt_int(el.get("minutes")),
                    bonus=_opt_int(el.get("bonus")),
                    goals_scored=_opt_int(el.get("goals_scored")),
                    assists=_opt_int(el.get("assists")),
                    xgi=_opt_float(el.get("xgi") or el.get("expected_goal_involvements")),
                    payload=payload,
                    ingested_at=_now(),
                )
            )
            stored += 1
        else:
            # Phase 21.1 fix: the update path previously refreshed only
            # total_points/payload — minutes/bonus/goals/assists kept whatever
            # a prior (possibly cross-season) writer had stored, silently
            # zeroing the form features built from this table.
            existing.total_points = total_points
            existing.minutes = _opt_int(el.get("minutes"))
            existing.bonus = _opt_int(el.get("bonus"))
            existing.goals_scored = _opt_int(el.get("goals_scored"))
            existing.assists = _opt_int(el.get("assists"))
            existing.xgi = _opt_float(el.get("xgi") or el.get("expected_goal_involvements"))
            existing.payload = payload
            existing.source = source
            existing.ingested_at = _now()

        player_id = players_by_element.get(element_id)
        if player_id is not None:
            team_id = _latest_team_for_player(db, player_id)
            if team_id is not None:
                perf = db.scalar(
                    select(PlayerGameweekPerformance).where(
                        PlayerGameweekPerformance.player_id == player_id,
                        PlayerGameweekPerformance.gameweek_id == gw_row.id,
                    )
                )
                if perf is None:
                    db.add(
                        PlayerGameweekPerformance(
                            player_id=player_id,
                            gameweek_id=gw_row.id,
                            season_id=gw_row.season_id,
                            team_id=team_id,
                            minutes=_opt_int(el.get("minutes")) or 0,
                            total_points=total_points,
                            bonus=_opt_int(el.get("bonus")) or 0,
                            goals_scored=_opt_int(el.get("goals_scored")) or 0,
                            assists=_opt_int(el.get("assists")) or 0,
                            ingested_at=_now(),
                            available_at=_now(),
                        )
                    )
                    mirrored += 1
                else:
                    # Phase 21.1 fix: refresh the full stat set, not just
                    # points/bonus — stale cross-season minutes otherwise
                    # poison the minutes_share in the baseline model.
                    perf.minutes = _opt_int(el.get("minutes")) or perf.minutes
                    perf.total_points = total_points
                    perf.goals_scored = _opt_int(el.get("goals_scored")) or perf.goals_scored
                    perf.assists = _opt_int(el.get("assists")) or perf.assists
                    if el.get("bonus") is not None:
                        perf.bonus = int(el["bonus"])
                    mirrored += 1
    db.flush()

    # 4 — reconcile ledger with actuals.
    actuals = {
        int(e["element_id"]): int(e.get("total_points") or 0)
        for e in elements
        if isinstance(e.get("element_id"), int) or str(e.get("element_id", "")).isdigit()
    }
    ledger_rows = db.execute(
        select(PredictionLedgerDB).where(PredictionLedgerDB.gameweek == gameweek)
    ).scalars().all()
    for row in ledger_rows:
        if row.actual is None and row.element_id in actuals:
            row.actual = actuals[row.element_id]
            row.reconciled_at = _now()
            ledger_filled += 1
    db.flush()

    scored = score_pending_recommendations(db, up_to_gameweek=gameweek)

    calibration = calibration_snapshot(db)
    return {
        "gameweek": gameweek,
        "stored": stored,
        "mirrored": mirrored,
        "predictions_captured": captured,
        "ledger_filled": ledger_filled,
        "recommendations_scored": scored,
        "calibration": calibration,
    }


def _opt_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def save_live_points(
    db: Session,
    gameweek: int,
    elements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Upsert per-player live points pushed during a matchday."""
    saved = updated = 0
    now = _now()
    for el in elements:
        try:
            element_id = int(el["element_id"])
        except (KeyError, TypeError, ValueError):
            continue
        points = _opt_int(el.get("points")) or 0
        row = db.scalar(
            select(SyncLivePointDB).where(
                SyncLivePointDB.gameweek == gameweek,
                SyncLivePointDB.element_id == element_id,
            )
        )
        if row is None:
            db.add(
                SyncLivePointDB(
                    gameweek=gameweek,
                    element_id=element_id,
                    points=points,
                    minutes=_opt_int(el.get("minutes")),
                    fixture_text=(el.get("fixture") or None),
                    opponent=(el.get("opponent") or None),
                    updated_at=now,
                )
            )
            saved += 1
        else:
            row.points = points
            if el.get("minutes") is not None:
                row.minutes = _opt_int(el.get("minutes"))
            if el.get("fixture"):
                row.fixture_text = str(el["fixture"])
            if el.get("opponent"):
                row.opponent = str(el["opponent"])
            row.updated_at = now
            updated += 1
    db.flush()
    return {"saved": saved, "updated": updated}


def record_recommendations(
    db: Session,
    session_key: str,
    report: Any,
) -> int:
    """Persist XI / captain / transfer / chip calls from a decisions report.

    Deduped per (session, gameweek, kind): re-running decisions for the same
    gameweek refreshes the subject instead of stacking duplicates.
    """
    created = 0
    now = _now()
    gameweek = int(report.gameweek)

    def _upsert(rec_type: str, subject: dict[str, Any], detail: dict[str, Any]) -> None:
        nonlocal created
        row = db.scalar(
            select(RecommendationDB).where(
                RecommendationDB.session_key == session_key,
                RecommendationDB.gameweek == gameweek,
                RecommendationDB.rec_type == rec_type,
            )
        )
        if row is None:
            db.add(
                RecommendationDB(
                    session_key=session_key,
                    gameweek=gameweek,
                    rec_type=rec_type,
                    subject=subject,
                    detail=detail,
                    created_at=now,
                )
            )
            created += 1
        else:
            row.subject = subject
            row.detail = detail

    xi = list(getattr(report, "starting_xi", []) or [])
    captain = getattr(report, "captain", None)
    vice = getattr(report, "vice_captain", None)
    tp = getattr(report, "transfer_plan", None)
    chip = getattr(report, "chip_recommendation", None)

    if xi:
        _upsert(
            "xi",
            {"xi": xi},
            {
                "bench_order": list(getattr(report, "bench_order", []) or []),
                "vice_captain": vice,
            },
        )
    if captain is not None:
        cap_pid = int(captain.player_id)
        alternatives = [pid for pid in xi if pid != cap_pid][:11]
        _upsert(
            "captain",
            {"captain_id": cap_pid},
            {
                "alternatives": alternatives,
                "expected_points": float(captain.expected_points or 0.0),
                "reason": getattr(captain, "main_reason", "") or "",
            },
        )
    if tp is not None and (getattr(tp, "transfers_in", None) or getattr(tp, "transfers_out", None)):
        _upsert(
            "transfer",
            {
                "transfers_in": list(tp.transfers_in or []),
                "transfers_out": list(tp.transfers_out or []),
            },
            {
                "hit_cost": int(getattr(tp, "hit_cost", 0) or 0),
                "expected_gain": float(getattr(tp, "expected_gain", 0.0) or 0.0),
                "reason": getattr(tp, "main_reason", "") or "",
            },
        )
    if chip is not None and getattr(chip, "chip_name", None):
        _upsert("chip", {"chip": chip.chip_name}, {"reason": chip.main_reason or ""})
    db.flush()
    return created


def _user_xi_for_gameweek(db: Session, session_key: str, gameweek: int) -> list[int]:
    """The user's actually-fielded XI for one gameweek (first 11 synced picks).

    The squad-push bookmarklet/Apps-Script trigger preserves FPL pick-slot
    order, so slots 1-11 of the squad snapshot whose ``gameweek`` matches the
    recommendation ARE the fielded XI. Returns ``[]`` when no matching
    snapshot exists — callers then grade NEUTRAL with an honest reason rather
    than leaving the row pending.
    """
    from fpl_intelligence.squad.models_db import SquadStateDB

    try:
        row = db.scalar(
            select(SquadStateDB).where(SquadStateDB.session_id == str(session_key))
        )
    except Exception:  # noqa: BLE001 - grading never crashes on read errors
        return []
    if row is None or not isinstance(row.squad_json, dict):
        return []
    squad = row.squad_json
    if int(squad.get("gameweek") or 0) != int(gameweek):
        return []
    ids = [int(p) for p in (squad.get("player_ids") or [])]
    return ids[:11]


def score_pending_recommendations(
    db: Session,
    *,
    up_to_gameweek: int,
    user_xi_resolver: Any | None = None,
) -> int:
    """Auto-score unscored recommendations whose gameweek has actuals.

    Phase 23 (C2): a row is NEVER left pending once its gameweek's results
    are ingested. When the math cannot produce a signed delta (missing
    actuals for a referenced player, unknown user XI) the row is graded
    NEUTRAL with an explicit reason instead.
    """
    pending = db.execute(
        select(RecommendationDB).where(
            RecommendationDB.scored_at.is_(None),
            RecommendationDB.gameweek <= up_to_gameweek,
        )
    ).scalars().all()
    if not pending:
        return 0
    actuals_by_gw: dict[int, dict[int, int]] = {}
    rows = db.execute(
        select(
            IngestedGameweekDB.gameweek,
            IngestedGameweekDB.element_id,
            IngestedGameweekDB.total_points,
        )
    ).all()
    for gw, element_id, pts in rows:
        actuals_by_gw.setdefault(int(gw), {})[int(element_id)] = int(pts or 0)

    resolve_xi = user_xi_resolver or (
        lambda key, gw: _user_xi_for_gameweek(db, key, gw)
    )

    def _unscoreable(reason: str) -> dict[str, Any]:
        return {"verdict": NEUTRAL, "delta": 0, "reason": reason}

    scored_count = 0
    now = _now()
    for rec in pending:
        actual = actuals_by_gw.get(rec.gameweek, {})
        result: dict[str, Any] | None = None
        if rec.rec_type == "captain":
            result = score_captain(
                int(rec.subject.get("captain_id", 0)),
                [int(p) for p in rec.detail.get("alternatives", [])],
                actual,
            )
            if result is None:
                result = _unscoreable(
                    "unscoreable: captain or alternative results missing"
                )
        elif rec.rec_type == "transfer":
            hit = int(rec.detail.get("hit_cost", 0) or 0)
            result = score_transfer(
                [int(p) for p in rec.subject.get("transfers_in", [])],
                [int(p) for p in rec.subject.get("transfers_out", [])],
                actual,
                hit_cost=hit,
            )
            if result is None:
                result = _unscoreable(
                    "unscoreable: transfer-in/out results missing"
                )
        elif rec.rec_type == "chip":
            # Chips are graded qualitatively until per-chip replay exists;
            # mark scored without inventing a delta.
            result = {
                "verdict": NEUTRAL,
                "delta": 0,
                "reason": "chip graded qualitatively — no per-chip replay yet",
            }
        elif rec.rec_type == "xi":
            user_xi = list(resolve_xi(rec.session_key, rec.gameweek)) or []
            result = score_xi(
                [int(p) for p in rec.subject.get("xi", [])], user_xi, actual
            )
            if result is None:
                result = _unscoreable(
                    "unscoreable: "
                    + (
                        "no synced squad snapshot for this gameweek"
                        if not user_xi
                        else "some XI players have no ingested results"
                    )
                )
        if result is None:
            continue
        rec.score = result
        rec.scored_at = now
        scored_count += 1
    db.flush()
    return scored_count


def _element_name_map(db: Session) -> dict[int, str]:
    """FPL element id -> web name, DB-first with the committed seed fallback.

    Track-record cards must read "Captain: Haaland", never "Captain #352" —
    so every id referenced by a recommendation is resolved through the player
    table and, for rows not yet mirrored, the bootstrap seed catalog.
    """
    names: dict[int, str] = {}
    for element_id, web_name in db.execute(
        select(Player.fpl_element_id, Player.web_name)
    ).all():
        if element_id is not None and web_name:
            names[int(element_id)] = str(web_name)
    if len(names) >= 100:
        return names
    try:
        from fpl_intelligence.prediction.live_provider import load_player_catalog

        for element_id, row in load_player_catalog().items():
            web_name = str(row.get("web_name") or "")
            if web_name and int(element_id) not in names:
                names[int(element_id)] = web_name
    except Exception as exc:  # noqa: BLE001 - display-only fallback
        logger.debug("seed-catalog name fallback failed: %s", exc)
    return names


def track_record_payload(db: Session, session_key: str) -> dict[str, Any]:
    """Full track-record read model for one entry (with resolved names)."""
    recs = db.execute(
        select(RecommendationDB)
        .where(RecommendationDB.session_key == session_key)
        .order_by(RecommendationDB.gameweek.desc(), RecommendationDB.created_at.desc())
    ).scalars().all()
    names = _element_name_map(db)

    def _nm(pid: Any) -> str | None:
        try:
            return names.get(int(pid))
        except (TypeError, ValueError):
            return None

    cards: list[dict[str, Any]] = []
    for r in recs:
        score = dict(r.score) if r.score else None
        subject = dict(r.subject) if r.subject else {}
        detail = dict(r.detail) if r.detail else {}

        # Phase 21.1: human-readable names on every card and score line.
        if r.rec_type == "captain":
            cap_name = _nm(subject.get("captain_id"))
            if cap_name:
                subject["captain_name"] = cap_name
            if score is not None:
                cap_name = _nm(score.get("captain")) or cap_name
                alt_name = _nm(score.get("best_alternative"))
                if cap_name:
                    score["captain_name"] = cap_name
                if alt_name:
                    score["alternative_name"] = alt_name
        elif r.rec_type == "transfer":
            in_names = [n for n in (_nm(p) for p in subject.get("transfers_in") or []) if n]
            out_names = [n for n in (_nm(p) for p in subject.get("transfers_out") or []) if n]
            if in_names:
                subject["transfers_in_names"] = in_names
            if out_names:
                subject["transfers_out_names"] = out_names
            if score is not None:
                s_in = [n for n in (_nm(p) for p in score.get("transfers_in") or []) if n]
                s_out = [n for n in (_nm(p) for p in score.get("transfers_out") or []) if n]
                if s_in:
                    score["transfers_in_names"] = s_in
                if s_out:
                    score["transfers_out_names"] = s_out
        elif r.rec_type == "xi":
            xi_names = [n for n in (_nm(p) for p in subject.get("xi") or []) if n]
            if xi_names:
                subject["xi_names"] = xi_names

        cards.append(
            {
                "gameweek": r.gameweek,
                "rec_type": r.rec_type,
                "subject": subject,
                "detail": detail,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "scored": r.scored_at is not None,
                "score": score,
            }
        )
    graded = [c["score"] for c in cards if c["score"]]
    hits = sum(1 for s in graded if s.get("verdict") in ("right", "neutral"))
    return {
        "entry_id": session_key,
        "cards": cards,
        "rolling": {
            "graded": len(graded),
            "hits": hits,
            "hit_rate": round(hits / len(graded), 3) if graded else None,
            "net_points": sum(int(s.get("delta") or 0) for s in graded),
            "last_5": [
                c
                for c in cards
                if c["score"] is not None
            ][:5],
        },
    }


def calibration_snapshot(db: Session) -> dict[str, Any]:
    """Aggregate predicted-vs-actual pairs plus the open forecast arms.

    Phase 23 (C3): "arms" are gameweeks whose per-player forecasts are
    already stored in the ledger but whose results have not been ingested yet
    (``actual IS NULL``). The Sources page renders them as
    "calibration arms: GW{n} forecasts stored" with a hint until graded.
    """
    rows = db.execute(
        select(PredictionLedgerDB.predicted, PredictionLedgerDB.actual).where(
            PredictionLedgerDB.actual.is_not(None)
        )
    ).all()
    payload = compute_calibration([(float(p), int(a)) for p, a in rows])
    arm_rows = db.execute(
        select(
            PredictionLedgerDB.gameweek,
            func.count(),
        )
        .where(PredictionLedgerDB.actual.is_(None))
        .group_by(PredictionLedgerDB.gameweek)
        .order_by(PredictionLedgerDB.gameweek)
    ).all()
    payload["forecast_arms"] = [
        {"gameweek": int(gw), "rows": int(n), "graded": False}
        for gw, n in arm_rows
        if n
    ]
    return payload
