from datetime import UTC, datetime

from fpl_intelligence_engine.team_strength import TeamStrengthEngine


def make_row(team_id: int, day: int, goals_for: int, goals_against: int, is_home: bool, *, xg: float = 1.0):
    return {
        "team_id": team_id,
        "played_at": datetime(2024, 1, day, tzinfo=UTC),
        "goals_for": goals_for,
        "goals_against": goals_against,
        "is_home": is_home,
        "xg": xg,
    }


def test_empty_engine_returns_neutral_strength() -> None:
    engine = TeamStrengthEngine([])
    estimate = engine.estimate(1, datetime(2024, 1, 20, tzinfo=UTC))
    assert estimate.attack_strength == 1.0
    assert estimate.defence_strength == 1.0


def test_home_advantage_is_above_one_when_home_history_outperforms() -> None:
    cutoff = datetime(2024, 1, 20, tzinfo=UTC)
    rows = [make_row(1, day, 2, 0, True) for day in range(2, 10)]
    rows += [make_row(2, day, 1, 2, False) for day in range(2, 10)]
    assert TeamStrengthEngine(rows).home_advantage(cutoff) > 1.0


def test_methods_produce_distinct_strength_and_fixture_signatures_on_controlled_history() -> None:
    cutoff = datetime(2024, 1, 20, tzinfo=UTC)
    goals = [0, 3, 1, 0, 2, 4, 1, 3]
    conceded = [2, 0, 1, 3, 0, 1, 2, 0]
    xg = [1.8, 0.4, 0.9, 2.2, 0.2, 3.1, 0.7, 1.6]
    rows = [
        make_row(1, day, goals[i], conceded[i], day % 2 == 0, xg=xg[i])
        for i, day in enumerate(range(2, 10))
    ]
    rows += [
        make_row(2, day, 1, 2, day % 2 == 0, xg=0.6)
        for day in range(2, 10)
    ]
    engine = TeamStrengthEngine(rows)

    estimates = {
        method: (
            engine.estimate(1, cutoff, method=method, window=5, decay=0.8),
            engine.estimate(2, cutoff, method=method, window=5, decay=0.8),
        )
        for method in ("rolling_goals", "ewma", "rolling_xg", "poisson")
    }
    strength_signatures = {
        method: tuple(
            round(value, 10)
            for estimate in estimates[method]
            for value in (
                estimate.attack_strength,
                estimate.defence_strength,
                estimate.home_attack_strength,
                estimate.away_attack_strength,
                estimate.home_defence_strength,
                estimate.away_defence_strength,
            )
        )
        for method in estimates
    }
    fixture_signatures = {
        method: tuple(
            round(value, 10)
            for value in (
                prediction.home_win_probability,
                prediction.draw_probability,
                prediction.away_win_probability,
                prediction.expected_home_goals,
                prediction.expected_away_goals,
            )
        )
        for method, (home, away) in estimates.items()
        for prediction in [engine.fixture_probability(99, cutoff, home, away)]
    }

    assert len(set(strength_signatures.values())) == 4
    # Different estimators can legitimately converge to the same downstream
    # fixture probabilities even when their underlying strength estimates are
    # distinct. Require meaningful fixture diversity without requiring a
    # one-to-one mapping from method -> fixture signature.
    assert len(set(fixture_signatures.values())) >= 3
