"""Phase 20.0 — fixture scanner math.

Pure functions only: normalisation, horizon selection, per-player runs,
squad swing score, and easiest team runs. No network, no DB.
"""

from __future__ import annotations

from fpl_intelligence.fixtures.scanner import (
    FixtureRow,
    average_fdr,
    easiest_team_runs,
    infer_current_gameweek,
    next_gameweeks,
    parse_fixtures,
    player_run,
    squad_swing_score,
    team_short_name,
)


def _raw_fixture(event, home, away, hd=2, ad=4, finished=False):
    return {
        "event": event,
        "team_h": home,
        "team_a": away,
        "team_h_difficulty": hd,
        "team_a_difficulty": ad,
        "finished": finished,
        "kickoff_time": "2026-08-22T11:30:00Z",
    }


class TestParseFixtures:
    def test_normalises_rows(self):
        rows = parse_fixtures([_raw_fixture(8, 1, 2, 3, 4)])
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, FixtureRow)
        assert (row.event, row.home_team, row.away_team) == (8, 1, 2)
        assert (row.home_difficulty, row.away_difficulty) == (3, 4)

    def test_drops_unassigned_rows(self):
        raw = _raw_fixture(None, 1, 2)
        assert parse_fixtures([raw]) == []

    def test_defaults_missing_fdr_to_neutral(self):
        raw = _raw_fixture(9, 3, 4)
        del raw["team_h_difficulty"]
        rows = parse_fixtures([raw])
        assert rows[0].home_difficulty == 3


class TestHorizon:
    def test_infer_current_from_unfinished(self):
        rows = parse_fixtures([
            _raw_fixture(7, 1, 2, finished=True),
            _raw_fixture(8, 3, 4),
            _raw_fixture(9, 5, 6),
        ])
        assert infer_current_gameweek(rows) == 8

    def test_next_gameweeks_window(self):
        rows = parse_fixtures([_raw_fixture(gw, 1, 2) for gw in range(8, 15)])
        assert next_gameweeks(rows, current_gw=8, count=5) == [8, 9, 10, 11, 12]

    def test_skips_blank_gameweeks(self):
        # GW10 has no fixtures at all (e.g. cup weekend).
        rows = parse_fixtures(
            [_raw_fixture(8, 1, 2), _raw_fixture(9, 1, 2), _raw_fixture(11, 1, 2)]
        )
        assert next_gameweeks(rows, current_gw=8, count=3) == [8, 9, 11]


class TestPlayerRun:
    def test_home_and_away_projection(self):
        rows = parse_fixtures([
            _raw_fixture(8, 1, 2, hd=2, ad=4),
            _raw_fixture(9, 3, 1, hd=5, ad=3),
        ])
        by_gw: dict[int, list] = {}
        for r in rows:
            by_gw.setdefault(r.event, []).append(r)

        runs = player_run(team_id=1, rows_by_gw=by_gw, horizon=[8, 9])
        assert runs[0].opponent == team_short_name(2)
        assert runs[0].is_home is True
        assert runs[0].difficulty == 2  # home difficulty used when at home
        assert runs[1].opponent == team_short_name(3)
        assert runs[1].is_home is False
        assert runs[1].difficulty == 3  # away difficulty used when away

    def test_blank_gw_gets_neutral_placeholder(self):
        by_gw = {8: parse_fixtures([_raw_fixture(8, 1, 2)])}
        runs = player_run(team_id=5, rows_by_gw=by_gw, horizon=[8, 9])
        assert runs[1].opponent_id == 0
        assert runs[1].difficulty == 3

    def test_average_fdr(self):
        rows = parse_fixtures([
            _raw_fixture(8, 1, 2, hd=2, ad=4),
            _raw_fixture(9, 3, 1, hd=5, ad=3),
        ])
        by_gw: dict[int, list] = {}
        for r in rows:
            by_gw.setdefault(r.event, []).append(r)
        runs = player_run(team_id=1, rows_by_gw=by_gw, horizon=[8, 9])
        real = [r for r in runs if r.opponent_id != 0]
        assert average_fdr(real) == 2.5


class TestSwingScore:
    def test_positive_for_easy_runs(self):
        # Every starter averages FDR 2 -> swing +(3-2)*n.
        assert squad_swing_score([2.0, 2.0]) == 2.0

    def test_negative_for_hard_runs(self):
        assert squad_swing_score([5.0]) == -2.0

    def test_neutral_when_all_three(self):
        assert squad_swing_score([3.0, 3.0, 3.0]) == 0.0


class TestEasiestRuns:
    def test_ranks_easiest_first_and_excludes_squad_clubs(self):
        rows = parse_fixtures([
            _raw_fixture(8, 1, 2, hd=5, ad=5),
            _raw_fixture(8, 3, 4, hd=2, ad=2),
            _raw_fixture(8, 6, 7, hd=2, ad=3),
        ])
        by_gw = {8: rows}
        out = easiest_team_runs(by_gw, [8], top=2, exclude_teams={1})
        names = [t.short_name for t in out]
        # Team 1 excluded; the two FDR-2 clubs rank first.
        assert team_short_name(1) not in names
        assert all(t.avg_fdr <= 3 for t in out)
        assert len(names) == 2

    def test_top_limit(self):
        rows = parse_fixtures([_raw_fixture(8, t, ((t % 20) + 1)) for t in range(1, 21)])
        by_gw = {8: rows}
        out = easiest_team_runs(by_gw, [8], top=5)
        assert len(out) == 5
