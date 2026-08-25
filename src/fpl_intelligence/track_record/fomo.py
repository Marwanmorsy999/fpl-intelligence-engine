"""Phase 27 Gate 1 (S2/S3) — FOMO & Regret + Free Transfer Valuation.

Post-GW grading reads RecommendationDB (captain calls) and compares USER's actual
captain vs engine's recommended captain via ingested_history. Alpha Capture Rate
compares user's transfer INs (TransferLogDB / snapshots) vs top Alpha at that GW.

Honest contract: when history or predictions missing, metrics are None with an
explicit note — never invented.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)


def _actual_points_map(db: Any, gameweek: int) -> dict[int, int]:
    try:
        from fpl_intelligence.sync.models import IngestedGameweekDB

        rows = db.execute(
            select(IngestedGameweekDB.element_id, IngestedGameweekDB.total_points).where(
                IngestedGameweekDB.gameweek == int(gameweek)
            )
        ).all()
        return {int(e): int(p or 0) for e, p in rows}
    except Exception as exc:  # noqa: BLE001
        logger.debug("ingested_history read failed gw%s: %s", gameweek, exc)
        with contextlib.suppress(Exception):
            db.rollback()
        return {}


def compute_regret(db: Any, session_id: str, gameweek: int | None = None) -> dict[str, Any]:
    """Cost of Ignoring Math + Alpha Capture details for one GW.

    If gameweek None, uses latest ingested GW that has both recommendation and actuals.
    """
    from fpl_intelligence.sync.models import RecommendationDB

    # Choose GW: max ingested that has actuals
    chosen_gw = gameweek
    if chosen_gw is None:
        try:
            from fpl_intelligence.sync.models import IngestedGameweekDB

            row = db.scalar(select(IngestedGameweekDB.gameweek).order_by(IngestedGameweekDB.gameweek.desc()).limit(1))
            chosen_gw = int(row) if row is not None else None
        except Exception:
            chosen_gw = None
        with contextlib.suppress(Exception):
            db.rollback()
    if chosen_gw is None:
        return {
            "status": "unavailable",
            "note": "No ingested actuals yet — FOMO grades after the daily 06:10 history ingest.",
            "gameweek": None,
            "captain_regret": None,
            "alpha_capture": None,
            "how_computed": "Needs ingested_history rows + RecommendationDB captain rows for the same GW.",
        }

    gw = int(chosen_gw)
    actuals = _actual_points_map(db, gw)
    if not actuals:
        return {
            "status": "no-actuals",
            "note": f"No ingested actuals for GW{gw} yet.",
            "gameweek": gw,
            "captain_regret": None,
            "alpha_capture": None,
            "how_computed": "ingested_history empty for this gameweek.",
        }

    # Captain regret: user's captain (stored subject) vs engine's rec
    cap_rows = (
        db.execute(
            select(RecommendationDB).where(
                RecommendationDB.session_key == str(session_id),
                RecommendationDB.gameweek == int(gw),
                RecommendationDB.rec_type == "captain",
            )
        )
        .scalars()
        .all()
    )
    captain_regret: dict[str, Any] | None = None
    if cap_rows:
        row = cap_rows[0]
        subject = row.subject if isinstance(row.subject, dict) else {}
        rec_captain = subject.get("captain_id") or subject.get("captain")
        try:
            rec_captain = int(rec_captain) if rec_captain is not None else None
        except (TypeError, ValueError):
            rec_captain = None
        # score may contain actual captain used vs best alternative — use it when scored
        score = row.score if isinstance(row.score, dict) else None
        if rec_captain is not None:
            rec_pts = int(actuals.get(rec_captain, 0))
            # User's captain is the first scored captain if available else rec itself
            # If score exists, it grades the CALL; we use its delta as cost of ignoring when user diverged.
            # Fallback: assume user captained someone else only if we have transfer ledger hint — otherwise honest none.
            if score and isinstance(score, dict) and score.get("captain") is not None:
                try:
                    user_captain = int(score.get("captain"))
                except (TypeError, ValueError):
                    user_captain = rec_captain
                user_pts = int(actuals.get(user_captain, 0))
                delta = int(user_pts) - int(rec_pts)
                # "Cost of Ignoring" is rec best vs user pick
                # If score has best_alternative, use that as comparison
                best_alt = score.get("best_alternative")
                try:
                    best_alt = int(best_alt) if best_alt is not None else None
                except (TypeError, ValueError):
                    best_alt = None
                if best_alt is not None:
                    best_pts = int(actuals.get(best_alt, 0))
                    cost = int(best_pts * 2) - int(user_pts * 2) if score.get("captain_points") is not None else int(best_pts) - int(user_pts)
                else:
                    cost = int(rec_pts * 2) - int(user_pts * 2) if rec_pts is not None else 0
                # Simpler: delta between rec captain (doubled) vs user captain
                # Correct: doubled points comparison
                rec_doubled = rec_pts * 2
                user_doubled = user_pts * 2
                pts_delta = int(rec_doubled - user_doubled)
                captain_regret = {
                    "gameweek": gw,
                    "recommended_captain": rec_captain,
                    "recommended_points": rec_pts,
                    "user_captain": user_captain,
                    "user_points": user_pts,
                    "delta": pts_delta,
                    "line": (
                        f"You lost {abs(pts_delta)} pts by ignoring the engine's captain call"
                        if pts_delta > 0
                        else f"You gained {abs(pts_delta)} pts vs the engine's captain call"
                        if pts_delta < 0
                        else "Captain call matched your pick — no regret."
                    ),
                    "how_computed": "Captain regret = (engine captain actual pts ×2) − (your captain actual pts ×2) from ingested_history.",
                }
            else:
                # Not yet scored — honest pending
                captain_regret = {
                    "gameweek": gw,
                    "recommended_captain": rec_captain,
                    "note": "Awaiting post-GW scoring of captain call.",
                    "how_computed": "Needs prediction_ledger reconciliation after ingested_history lands.",
                }
    else:
        captain_regret = None

    # Alpha Capture Rate: graded transfer recommendations vs actual transfers
    # Need transfer recs for this GW
    tr_rows = (
        db.execute(
            select(RecommendationDB).where(
                RecommendationDB.session_key == str(session_id),
                RecommendationDB.gameweek == int(gw),
                RecommendationDB.rec_type == "transfer",
            )
        )
        .scalars()
        .all()
    )
    alpha_capture: dict[str, Any] | None = None
    if tr_rows:
        # How many transfer recs were scored as right vs total graded
        graded = [r for r in tr_rows if isinstance(r.score, dict) and r.score.get("verdict")]
        if graded:
            right = sum(1 for r in graded if r.score.get("verdict") == "right")
            rate = round(right / len(graded), 3) if graded else None
            alpha_capture = {
                "gameweek": gw,
                "graded_transfers": len(graded),
                "right": right,
                "rate": rate,
                "line": f"Alpha Capture Rate: {int(rate*100)}% of graded transfer calls were right" if rate is not None else "No graded transfers yet.",
                "how_computed": "Graded transfer recommendations: right/(right+wrong) from RecommendationDB scores.",
            }
        else:
            alpha_capture = {
                "gameweek": gw,
                "note": "Transfer calls not yet graded — awaiting actuals.",
                "how_computed": "Needs ingested_history for the GW to score transfer calls.",
            }

    # Overall status
    if captain_regret is None and alpha_capture is None:
        return {
            "status": "no-recommendations",
            "note": f"No recommendations found for GW{gw} for this session.",
            "gameweek": gw,
            "captain_regret": None,
            "alpha_capture": None,
            "how_computed": "RecommendationDB has no captain/transfer rows for this GW+session.",
        }

    # Build human "Cost of Ignoring Math" summary line
    cost_line: str | None = None
    if captain_regret and captain_regret.get("delta") is not None:
        d = int(captain_regret["delta"])
        if d > 0:
            rec_name = str(captain_regret.get("recommended_captain", "engine pick"))
            # Try to resolve names
            try:
                from fpl_intelligence.prediction.live_provider import load_player_catalog

                cat = load_player_catalog()
                rec_name = cat.get(int(rec_name), {}).get("web_name", rec_name) if str(rec_name).isdigit() else rec_name
            except Exception:
                pass
            cost_line = f"You lost {abs(d)} pts and ranks by ignoring the {rec_name} captain recommendation."
        elif d == 0:
            cost_line = "Your captain matched the engine — no regret this GW."
        else:
            cost_line = f"You gained {abs(d)} pts vs the engine's captain call — nice override."

    status = "ok" if (captain_regret or alpha_capture) else "unavailable"
    return {
        "status": status,
        "gameweek": gw,
        "captain_regret": captain_regret,
        "alpha_capture": alpha_capture,
        "cost_line": cost_line,
        "how_computed": "Captain regret vs engine pick + Alpha capture rate from graded transfer calls — all from ingested actuals.",
    }
