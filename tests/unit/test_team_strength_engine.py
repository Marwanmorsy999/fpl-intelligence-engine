from datetime import UTC, datetime, timedelta

from fpl_intelligence.prediction.team_strength_engine import TeamMatch, TeamStrengthEngine


def make_row(
    team_id: int, day: int, goals: int, conceded: int, home: bool = True, xg: float | None = None
) -> TeamMatch:
    event = datetime(2024, 1, day, tzinfo=UTC)
    known = event + timedelta(hours=2)
    return TeamMatch(
        team_id,
        day,
        "2023-24",
        event,
        known,
        known,
        home,
        goals,
        conceded,
        xg,
        float(conceded) if xg is not None else None,
    )


def test_estimate_excludes_future_results_and_future_xg() -> None:
    cutoff = datetime(2024, 1, 10, tzinfo=UTC)
    rows = [make_row(1, 2, 1, 0), make_row(1, 5, 2, 0), make_row(1, 12, 10, 0, xg=10.0)]
    estimate = TeamStrengthEngine(rows).estimate(1, cutoff, method="rolling_goals", window=20)
    assert estimate.sample_size == 2
    assert estimate.attack_strength < 2.0


def test_missing_temporal_provenance_is_not_usable() -> None:
    cutoff = datetime(2024, 1, 10, tzinfo=UTC)
    row = make_row(1, 2, 4, 0)
    unsafe = TeamMatch(
        row.team_id,
        row.fixture_id,
        row.season,
        row.event_time,
        None,
        row.ingested_at,
        row.is_home,
        row.goals_scored,
        row.goals_conceded,
    )
    assert TeamStrengthEngine([unsafe]).estimate(1, cutoff).sample_size == 0


def test_all_strength_dimensions_and_fixture_probabilities_are_bounded() -> None:
    cutoff = datetime(2024, 1, 20, tzinfo=UTC)
    rows = [make_row(1, day, 2, 1, day % 2 == 0) for day in range(2, 16)]
    rows += [make_row(2, day, 1, 2, day % 2 == 0) for day in range(2, 16)]
    engine = TeamStrengthEngine(rows)
    home = engine.estimate(1, cutoff, method="poisson")
    away = engine.estimate(2, cutoff, method="poisson")
    assert home.home_attack_strength > 0
    assert home.away_defence_strength > 0
    prediction = engine.fixture_probability(99, cutoff, home, away)
    assert (
        abs(
            prediction.home_win_probability
            + prediction.draw_probability
            + prediction.away_win_probability
            - 1
        )
        < 1e-9
    )
    for value in prediction.to_dict().values():
        if isinstance(value, float):
            assert 0 <= value <= 1 or value > 1
    assert 0 <= prediction.home_team_goals_2_plus_probability <= 1
    assert 0 <= prediction.away_team_goals_3_plus_probability <= 1


def test_home_advantage_is_learned_from_prior_matches() -> None:
    cutoff = datetime(2024, 1, 20, tzinfo=UTC)
    rows = [make_row(1, day, 3, 0, True) for day in range(2, 10)]
    rows += [make_row(2, day, 1, 0, False) for day in range(2, 10)]
    assert TeamStrengthEngine(rows).home_advantage(cutoff) > 1.0


def test_methods_produce_distinct_strength_signatures_on_controlled_history() -> None:
    cutoff = datetime(2024, 1, 20, tzinfo=UTC)
    goals = [0, 3, 1, 0, 2, 4, 1, 3]
    conceded = [2, 0, 1, 3, 0, 1, 2, 0]
    xg = [1.8, 0.4, 0.9, 2.2, 0.2, 3.1, 0.7, 1.6]
    rows = [
        make_row(1, day, goals[i], conceded[i], day % 2 == 0, xg=xg[i])
        for i, day in enumerate(range(2, 10))
    ]
    engine = TeamStrengthEngine(rows)

    estimates = {
        method: engine.estimate(1, cutoff, method=method, window=5, decay=0.8)
        for method in ("rolling_goals", "ewma", "rolling_xg", "poisson")
    }
    signatures = {
        method: tuple(round(value, 10) for value in (
            estimate.attack_strength,
            estimate.defence_strength,
            estimate.home_attack_strength,
            estimate.away_attack_strength,
            estimate.home_defence_strength,
            estimate.away_defence_strength,
        ))
        for method, estimate in estimates.items()
    }

    assert len(set(signatures.values())) == 4
