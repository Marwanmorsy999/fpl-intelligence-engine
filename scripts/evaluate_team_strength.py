"""Evaluate Stage 2B.1 on real canonical historical PostgreSQL data only.

This command is read-only against the validation database. It excludes the
locked 2025-26 holdout and all 2026-27 rows, and writes only a local JSON
artifact under data/experiments/.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func, select

from fpl_intelligence.db.models import Fixture, Season, TeamMatchPerformance
from fpl_intelligence.db.session import validation_session_factory
from fpl_intelligence.prediction.team_strength_engine import TeamStrengthEngine

SEASONS = ("2022-23", "2023-24", "2024-25")
METHODS = ("rolling_goals", "ewma", "rolling_xg", "poisson")


def logloss(probability: float, outcome: int) -> float:
    return -math.log(max(1e-15, min(1 - 1e-15, probability if outcome else 1 - probability)))


def _team_source_counts(db, season_ids: list[int]) -> dict[str, int]:
    if not season_ids:
        return {"rows": 0, "temporally_usable": 0}
    historical = TeamMatchPerformance.season_id.in_(season_ids)
    return {
        "rows": int(
            db.scalar(select(func.count()).select_from(TeamMatchPerformance).where(historical)) or 0
        ),
        "temporally_usable": int(
            db.scalar(
                select(func.count())
                .select_from(TeamMatchPerformance)
                .where(
                    historical,
                    TeamMatchPerformance.available_at.is_not(None),
                    TeamMatchPerformance.ingested_at.is_not(None),
                )
            )
            or 0
        ),
    }


def _validate_team_source(db, seasons: dict[int, Season]) -> dict[str, object]:
    counts_by_season: dict[str, int] = {}
    usable_by_season: dict[str, int] = {}
    for season in seasons.values():
        counts_by_season[season.code] = int(
            db.scalar(
                select(func.count())
                .select_from(TeamMatchPerformance)
                .where(TeamMatchPerformance.season_id == season.id)
            )
            or 0
        )
        usable_by_season[season.code] = int(
            db.scalar(
                select(func.count())
                .select_from(TeamMatchPerformance)
                .where(
                    TeamMatchPerformance.season_id == season.id,
                    TeamMatchPerformance.available_at.is_not(None),
                    TeamMatchPerformance.ingested_at.is_not(None),
                )
            )
            or 0
        )

    missing = [season for season in SEASONS if counts_by_season.get(season, 0) == 0]
    temporal_gaps = [
        season
        for season in SEASONS
        if counts_by_season.get(season, 0) != usable_by_season.get(season, 0)
    ]
    if missing:
        raise RuntimeError(
            "Team Strength validation source is empty for canonical historical season(s): "
            + ", ".join(missing)
            + ". The previous evaluator silently converted this into constant fallback predictions."
        )
    if temporal_gaps:
        raise RuntimeError(
            "Team Strength validation has unusable temporal provenance for season(s): "
            + ", ".join(temporal_gaps)
        )

    total_rows = sum(counts_by_season.values())
    return {
        "source": "team_match_performances",
        "rows": total_rows,
        "temporally_usable": sum(usable_by_season.values()),
        "rows_by_season": counts_by_season,
    }


def evaluate_method(
    engine: TeamStrengthEngine, fixtures: list[Fixture], method: str
) -> dict[str, object]:
    rows: list[dict[str, float | str]] = []
    for fixture in sorted(fixtures, key=lambda item: item.kickoff_time):
        assert fixture.kickoff_time is not None
        home = engine.estimate(fixture.home_team_id, fixture.kickoff_time, method=method)
        away = engine.estimate(fixture.away_team_id, fixture.kickoff_time, method=method)
        prediction = engine.fixture_probability(fixture.id, fixture.kickoff_time, home, away)
        actual_home, actual_away = fixture.home_score or 0, fixture.away_score or 0
        rows.append(
            {
                "season": fixture.season.code,
                "home_mae": abs(prediction.expected_home_goals - actual_home),
                "away_mae": abs(prediction.expected_away_goals - actual_away),
                "home_sq": (prediction.expected_home_goals - actual_home) ** 2,
                "away_sq": (prediction.expected_away_goals - actual_away) ** 2,
                "result_ll": logloss(
                    prediction.home_win_probability
                    if actual_home > actual_away
                    else prediction.away_win_probability
                    if actual_home < actual_away
                    else prediction.draw_probability,
                    1,
                ),
                "home_brier": (prediction.home_win_probability - int(actual_home > actual_away))
                ** 2,
                "home_cs_brier": (prediction.home_clean_sheet_probability - int(actual_away == 0))
                ** 2,
                "home_cs_ll": logloss(
                    prediction.home_clean_sheet_probability, int(actual_away == 0)
                ),
                "home_2_ll": logloss(
                    prediction.home_team_goals_2_plus_probability, int(actual_home >= 2)
                ),
                "home_3_ll": logloss(
                    prediction.home_team_goals_3_plus_probability, int(actual_home >= 3)
                ),
                "home_win_probability": prediction.home_win_probability,
                "home_win_actual": int(actual_home > actual_away),
            }
        )

    def mean(key: str) -> float:
        return sum(float(row[key]) for row in rows) / max(len(rows), 1)

    reliability = []
    for lower in [i / 10 for i in range(10)]:
        bucket = [row for row in rows if lower <= float(row["home_win_probability"]) < lower + 0.1]
        if bucket:
            reliability.append(
                {
                    "bin": lower,
                    "n": len(bucket),
                    "predicted": mean_bucket(bucket, "home_win_probability"),
                    "observed": mean_bucket(bucket, "home_win_actual"),
                }
            )
    return {
        "n_fixtures": len(rows),
        "mae": (mean("home_mae") + mean("away_mae")) / 2,
        "rmse": math.sqrt((mean("home_sq") + mean("away_sq")) / 2),
        "multiclass_log_loss": mean("result_ll"),
        "home_win_brier": mean("home_brier"),
        "clean_sheet_brier": mean("home_cs_brier"),
        "clean_sheet_log_loss": mean("home_cs_ll"),
        "goals_2_plus_log_loss": mean("home_2_ll"),
        "goals_3_plus_log_loss": mean("home_3_ll"),
        "reliability": reliability,
    }


def mean_bucket(bucket: list[dict[str, float | str]], key: str) -> float:
    return sum(float(row[key]) for row in bucket) / len(bucket)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", default="data/experiments/team_strength/stage-2b1-evaluation.json"
    )
    args = parser.parse_args()
    SessionLocal = validation_session_factory()
    with SessionLocal() as db:
        seasons = {
            season.id: season
            for season in db.scalars(select(Season)).all()
            if season.code in SEASONS
        }
        source_stats = _validate_team_source(db, seasons)
        fixtures = [
            fixture
            for fixture in db.scalars(select(Fixture)).all()
            if fixture.season_id in seasons
            and fixture.home_score is not None
            and fixture.away_score is not None
            and fixture.kickoff_time is not None
        ]
        if not fixtures:
            raise RuntimeError(
                "No real canonical fixtures found for 2022-23..2024-25; "
                "refusing synthetic validation."
            )
        engine = TeamStrengthEngine.from_db(db, season_codes=SEASONS)
        if not engine.rows:
            raise RuntimeError(
                "Team Strength engine loaded zero temporally eligible TeamMatch rows; "
                "refusing to evaluate fallback predictions."
            )
        report = {
            "seasons": SEASONS,
            "n_fixtures": len(fixtures),
            "data_source": source_stats,
            "methods": {method: evaluate_method(engine, fixtures, method) for method in METHODS},
        }
        by_season = defaultdict(list)
        for fixture in fixtures:
            by_season[fixture.season.code].append(fixture)
        report["by_season"] = {
            season: {method: evaluate_method(engine, rows, method) for method in METHODS}
            for season, rows in by_season.items()
        }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(
        json.dumps(
            {"output": str(output), "n_fixtures": len(fixtures), "methods": list(METHODS)}, indent=2
        )
    )


if __name__ == "__main__":
    main()
