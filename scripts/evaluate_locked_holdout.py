"""Evaluate frozen Minutes and Team Strength candidates on locked 2025-26."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fpl_intelligence.backtesting.cutoff import get_all_gameweek_cutoffs  # noqa: E402
from fpl_intelligence.config.holdout import (  # noqa: E402
    DEFAULT_SEASON_SPLIT,
    DEVELOPMENT_SEASONS,
    FINAL_HOLDOUT_SEASONS,
    HoldoutMode,
)
from fpl_intelligence.db.models import (  # noqa: E402
    Fixture,
    Gameweek,
    PlayerGameweekPerformance,
    Season,
)
from fpl_intelligence.db.session import validation_session_factory  # noqa: E402
from fpl_intelligence.features.temporal import DEFAULT_POLICY  # noqa: E402
from fpl_intelligence.prediction.minutes import SimpleRecentMinutesBaseline  # noqa: E402
from fpl_intelligence.prediction.minutes_validation_fast import (  # noqa: E402
    FastMinutesWalkForwardEvaluator,
    _PerfRow,
)
from fpl_intelligence.prediction.team_strength_engine import TeamStrengthEngine  # noqa: E402

HOLDOUT = FINAL_HOLDOUT_SEASONS[0]
TEAM_METHOD = "ewma"
TEAM_WINDOW = 5
TEAM_DECAY = 0.9


def _logloss(probability: float, actual: int) -> float:
    p = max(1e-15, min(1 - 1e-15, probability))
    return -math.log(p if actual else 1 - p)


def _minutes_holdout(db) -> dict[str, object]:
    """Fit Minutes only on development data and score frozen parameters on holdout."""
    dev_cutoffs = []
    for season in DEVELOPMENT_SEASONS:
        dev_cutoffs.extend(get_all_gameweek_cutoffs(db, season, policy=DEFAULT_POLICY))
    dev_cutoffs.sort(key=lambda c: c.cutoff_time)
    if not dev_cutoffs:
        raise RuntimeError("no development folds available for frozen Minutes fit")

    evaluator = FastMinutesWalkForwardEvaluator(db, feature_version="2.0.0")
    dev_datasets = evaluator._build_datasets_once(dev_cutoffs)
    # Warm-up folds are excluded from development scoring, but remain legitimate
    # historical training data for the final frozen fit.
    model = evaluator._fit_model(dev_datasets)

    holdout_cutoffs = get_all_gameweek_cutoffs(db, HOLDOUT, policy=DEFAULT_POLICY)
    holdout_cutoffs.sort(key=lambda c: c.cutoff_time)
    if not holdout_cutoffs:
        raise RuntimeError("no 2025-26 holdout cutoffs available")

    stmt = (
        select(
            PlayerGameweekPerformance.player_id,
            PlayerGameweekPerformance.gameweek_id,
            PlayerGameweekPerformance.minutes,
            PlayerGameweekPerformance.total_points,
            PlayerGameweekPerformance.goals_scored,
            PlayerGameweekPerformance.assists,
            PlayerGameweekPerformance.available_at,
            PlayerGameweekPerformance.ingested_at,
            Gameweek.provider_event_id,
            Season.code,
        )
        .join(Gameweek, Gameweek.id == PlayerGameweekPerformance.gameweek_id)
        .join(Season, Season.id == Gameweek.season_id)
        .where(
            Season.code.in_([*DEVELOPMENT_SEASONS, HOLDOUT]),
            PlayerGameweekPerformance.available_at.is_not(None),
            PlayerGameweekPerformance.ingested_at.is_not(None),
        )
        .order_by(
            PlayerGameweekPerformance.player_id,
            Gameweek.provider_event_id,
            PlayerGameweekPerformance.gameweek_id,
        )
    )
    raw = db.execute(stmt).all()
    histories: dict[int, list[_PerfRow]] = defaultdict(list)
    target_rows: dict[int, dict[int, float]] = defaultdict(dict)
    for row in raw:
        perf = _PerfRow(
            player_id=int(row.player_id),
            gameweek_id=int(row.gameweek_id),
            minutes=float(row.minutes) if row.minutes is not None else None,
            total_points=float(row.total_points) if row.total_points is not None else None,
            goals_scored=float(row.goals_scored) if row.goals_scored is not None else None,
            assists=float(row.assists) if row.assists is not None else None,
            available_at=row.available_at,
            ingested_at=row.ingested_at,
        )
        histories[perf.player_id].append(perf)
        if row.code == HOLDOUT and row.minutes is not None:
            target_rows[int(row.provider_event_id)][int(row.player_id)] = float(row.minutes)

    for rows in histories.values():
        rows.sort(key=lambda r: (r.available_at, r.ingested_at, r.gameweek_id))

    candidate_rows: list[tuple[float, float, int, float]] = []
    baseline_rows: list[tuple[float, float, int, float]] = []
    folds: list[dict[str, object]] = []
    excluded = 0

    for cutoff in holdout_cutoffs:
        targets = target_rows.get(cutoff.gameweek, {})
        features: dict[int, dict[str, float]] = {}
        for player_id in sorted(targets):
            eligible = [
                row
                for row in histories.get(player_id, [])
                if row.available_at <= cutoff.cutoff_time
                and row.ingested_at <= cutoff.cutoff_time
            ]
            if not eligible:
                excluded += 1
                continue
            features[player_id] = evaluator._feature_builder(eligible)

        predictions = model.predict_batch(
            features,
            cutoff=cutoff.cutoff_time,
            context={"cutoff_time": cutoff.cutoff_time.isoformat()},
        )
        baseline = SimpleRecentMinutesBaseline().predict_batch(features, cutoff)
        for player_id, actual in targets.items():
            pred = predictions.get(player_id)
            base = baseline.get(player_id)
            if pred is None or base is None:
                continue
            started = int(actual >= 60)
            candidate_rows.append(
                (float(pred["expected_minutes"]), actual, started, float(pred["probability_starting"]))
            )
            baseline_rows.append(
                (float(base["expected_minutes"]), actual, started, float(base["probability_starting"]))
            )
        folds.append(
            {
                "gameweek": cutoff.gameweek,
                "cutoff": cutoff.cutoff_time.isoformat(),
                "n_targets": len(targets),
                "n_evaluated": sum(1 for player_id in targets if player_id in predictions),
            }
        )

    def metrics(rows: list[tuple[float, float, int, float]]) -> dict[str, float | int]:
        if not rows:
            raise RuntimeError("no evaluable holdout Minutes rows")
        return {
            "n": len(rows),
            "mae": round(mean(abs(pred - actual) for pred, actual, *_ in rows), 6),
            "rmse": round(math.sqrt(mean((pred - actual) ** 2 for pred, actual, *_ in rows)), 6),
            "start_brier": round(
                mean((prob - started) ** 2 for pred, actual, started, prob in rows), 6
            ),
            "start_log_loss": round(
                mean(_logloss(prob, started) for pred, actual, started, prob in rows), 6
            ),
        }

    candidate_metrics = metrics(candidate_rows)
    baseline_metrics = metrics(baseline_rows)
    return {
        "model": "minutes_model",
        "model_version": "2.0.0",
        "feature_version": "2.0.0",
        "training_seasons": list(DEVELOPMENT_SEASONS),
        "holdout_season": HOLDOUT,
        "training_rows": sum(len(dataset.targets) for _, dataset in dev_datasets),
        "excluded_target_rows_without_history": excluded,
        "candidate": candidate_metrics,
        "baseline_recent_minutes": baseline_metrics,
        "beats_baseline_mae": candidate_metrics["mae"] < baseline_metrics["mae"],
        "beats_baseline_start_brier": candidate_metrics["start_brier"] < baseline_metrics["start_brier"],
        "promotion_gate_passed": False,
        "folds": folds,
    }


def _team_holdout(db) -> dict[str, object]:
    """Evaluate frozen EWMA chronologically on 2025-26."""
    season = db.scalar(select(Season).where(Season.code == HOLDOUT))
    if season is None:
        raise RuntimeError("2025-26 holdout season is missing")
    fixtures = list(
        db.scalars(
            select(Fixture)
            .where(
                Fixture.season_id == season.id,
                Fixture.kickoff_time.is_not(None),
                Fixture.home_score.is_not(None),
                Fixture.away_score.is_not(None),
            )
            .order_by(Fixture.kickoff_time, Fixture.id)
        ).all())
    if len(fixtures) != 380:
        raise RuntimeError(f"expected 380 scored holdout fixtures, found {len(fixtures)}")

    engine = TeamStrengthEngine.from_db(db, season_codes=[*DEVELOPMENT_SEASONS, HOLDOUT])
    candidate_rows = []
    baseline_rows = []
    for fixture in fixtures:
        cutoff = fixture.kickoff_time
        assert cutoff is not None
        home = engine.estimate(
            fixture.home_team_id, cutoff, method=TEAM_METHOD, window=TEAM_WINDOW, decay=TEAM_DECAY
        )
        away = engine.estimate(
            fixture.away_team_id, cutoff, method=TEAM_METHOD, window=TEAM_WINDOW, decay=TEAM_DECAY
        )
        pred = engine.fixture_probability(fixture.id, cutoff, home, away)
        actual_home = int(fixture.home_score or 0)
        actual_away = int(fixture.away_score or 0)
        actual_result = 1 if actual_home > actual_away else -1 if actual_home < actual_away else 0
        result_prob = (
            pred.home_win_probability
            if actual_result == 1
            else pred.away_win_probability
            if actual_result == -1
            else pred.draw_probability
        )
        candidate_rows.append(
            {
                "mae": (abs(pred.expected_home_goals - actual_home) + abs(pred.expected_away_goals - actual_away)) / 2,
                "sq": ((pred.expected_home_goals - actual_home) ** 2 + (pred.expected_away_goals - actual_away) ** 2) / 2,
                "result_ll": _logloss(result_prob, 1),
                "home_brier": (pred.home_win_probability - int(actual_result == 1)) ** 2,
                "home_cs_brier": (pred.home_clean_sheet_probability - int(actual_away == 0)) ** 2,
            }
        )

        baseline_home = engine.estimate(
            fixture.home_team_id, cutoff, method="rolling_goals", window=TEAM_WINDOW, decay=TEAM_DECAY
        )
        baseline_away = engine.estimate(
            fixture.away_team_id, cutoff, method="rolling_goals", window=TEAM_WINDOW, decay=TEAM_DECAY
        )
        base_pred = engine.fixture_probability(fixture.id, cutoff, baseline_home, baseline_away)
        base_result_prob = (
            base_pred.home_win_probability
            if actual_result == 1
            else base_pred.away_win_probability
            if actual_result == -1
            else base_pred.draw_probability
        )
        baseline_rows.append(
            {
                "mae": (abs(base_pred.expected_home_goals - actual_home) + abs(base_pred.expected_away_goals - actual_away)) / 2,
                "sq": ((base_pred.expected_home_goals - actual_home) ** 2 + (base_pred.expected_away_goals - actual_away) ** 2) / 2,
                "result_ll": _logloss(base_result_prob, 1),
                "home_brier": (base_pred.home_win_probability - int(actual_result == 1)) ** 2,
                "home_cs_brier": (base_pred.home_clean_sheet_probability - int(actual_away == 0)) ** 2,
            }
        )

    def metrics(rows: list[dict[str, float]]) -> dict[str, float | int]:
        return {
            "n": len(rows),
            "mae": round(mean(r["mae"] for r in rows), 6),
            "rmse": round(math.sqrt(mean(r["sq"] for r in rows)), 6),
            "multiclass_log_loss": round(mean(r["result_ll"] for r in rows), 6),
            "home_win_brier": round(mean(r["home_brier"] for r in rows), 6),
            "clean_sheet_brier": round(mean(r["home_cs_brier"] for r in rows), 6),
        }

    candidate = metrics(candidate_rows)
    baseline = metrics(baseline_rows)
    return {
        "model": "team_strength_engine",
        "model_version": "2.0.0",
        "feature_version": "team-strength-2.0.0",
        "method": TEAM_METHOD,
        "window": TEAM_WINDOW,
        "decay": TEAM_DECAY,
        "training_seasons": list(DEVELOPMENT_SEASONS),
        "holdout_season": HOLDOUT,
        "candidate": candidate,
        "baseline_rolling_goals": baseline,
        "candidate_better_on_mae": candidate["mae"] < baseline["mae"],
        "candidate_better_on_rmse": candidate["rmse"] < baseline["rmse"],
        "candidate_better_on_log_loss": candidate["multiclass_log_loss"] < baseline["multiclass_log_loss"],
        "candidate_better_on_home_win_brier": candidate["home_win_brier"] < baseline["home_win_brier"],
        "promotion_gate_passed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/experiments/holdout/2025-26-frozen-evaluation.json")
    args = parser.parse_args()

    DEFAULT_SEASON_SPLIT.validate_observation(
        season=HOLDOUT,
        mode=HoldoutMode.FINAL_HOLDOUT_EVALUATION,
    )
    with validation_session_factory()() as db:
        season = db.scalar(select(Season).where(Season.code == HOLDOUT))
        if season is None:
            raise RuntimeError("locked 2025-26 holdout season is not present")
        report = {
            "holdout_policy": {
                "mode": HoldoutMode.FINAL_HOLDOUT_EVALUATION,
                "holdout_season": HOLDOUT,
                "development_seasons": list(DEVELOPMENT_SEASONS),
                "model_selection_frozen": True,
                "evaluation_read_only": True,
            },
            "minutes": _minutes_holdout(db),
            "team_strength": _team_holdout(db),
        }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "holdout": HOLDOUT}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
