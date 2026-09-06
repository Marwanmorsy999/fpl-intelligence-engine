"""Runtime activation of holdout-approved Team Strength EWMA."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fpl_intelligence.prediction.team_strength_engine import TeamMatch, TeamStrengthEngine
from fpl_intelligence.prediction.team_strength_live import (
    TS_DECAY,
    TS_METHOD,
    TS_MODEL_VERSION,
    TS_WINDOW,
    apply_multipliers_to_points,
    player_team_map_from_catalog,
)


def test_frozen_holdout_hyperparameters() -> None:
    assert TS_METHOD == "ewma"
    assert TS_WINDOW == 5
    assert TS_DECAY == 0.9
    assert TS_MODEL_VERSION == "2.0.0"


def test_apply_multipliers_scales_by_team() -> None:
    points = {1: 4.0, 2: 6.0, 3: 5.0}
    team_by_player = {1: 10, 2: 20, 3: 99}
    multipliers = {10: 1.2, 20: 0.8}
    out = apply_multipliers_to_points(points, team_by_player, multipliers)
    assert out[1] == 4.8
    assert out[2] == 4.8
    assert out[3] == 5.0


def test_player_team_map_from_catalog() -> None:
    catalog = {1: {"team": 7}, 2: {"team": None}, 3: {"web_name": "X"}}
    assert player_team_map_from_catalog(catalog) == {1: 7}


def test_ewma_fixture_lambdas_differ_for_strong_vs_weak() -> None:
    cutoff = datetime(2024, 1, 20, tzinfo=UTC)
    rows: list[TeamMatch] = []
    for day in range(2, 12):
        event = datetime(2024, 1, day, tzinfo=UTC)
        known = event + timedelta(hours=2)
        rows.append(
            TeamMatch(1, day, "2023-24", event, known, known, True, 3.0, 0.5, None, None)
        )
    for day in range(2, 12):
        event = datetime(2024, 1, day, tzinfo=UTC)
        known = event + timedelta(hours=2)
        rows.append(
            TeamMatch(2, 100 + day, "2023-24", event, known, known, False, 0.5, 3.0, None, None)
        )
    engine = TeamStrengthEngine(rows)
    home = engine.estimate(1, cutoff, method="ewma", window=5, decay=0.9)
    away = engine.estimate(2, cutoff, method="ewma", window=5, decay=0.9)
    fp = engine.fixture_probability(999, cutoff, home, away)
    assert fp.expected_home_goals > fp.expected_away_goals
    assert home.sample_size > 0
    assert away.sample_size > 0
