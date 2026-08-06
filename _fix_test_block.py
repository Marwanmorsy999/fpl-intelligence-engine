path = "tests/unit/test_phase7_availability.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

# The MiniProvider block section got mangled. Replace from the create that
# precedes MiniProvider through end of file with correctly-indented version.
marker = "        db_session.commit()\n        pids = [p.id for p in players]"
# Find the broken region: the line "db_session.commit()" at column 0
broken_start = content.find("\ndb_session.commit()\n        pids = [p.id for p in players]")

if broken_start == -1:
    print("BROKEN_PATTERN_NOT_FOUND")
else:
    # Everything from broken_start+1 to end needs to be re-indented properly.
    # We'll replace the whole block from the broken commit line to EOF.
    prefix = content[:broken_start]
    block = """        db_session.commit()
        pids = [p.id for p in players]

        class MiniProvider(DecisionPredictionProvider):
            def __init__(self, captain_idx):
                # captain_idx selects which player gets the highest EV and is
                # therefore chosen as captain by the backtester.
                self._captain_idx = captain_idx

            def _pred(self, pid, gw):
                idx = pids.index(pid)
                # Give the chosen captain the highest expected points so the
                # backtester selects it as captain (different actual points).
                pts = 10.0 if idx == self._captain_idx else 1.0
                return PlayerPrediction(
                    player_id=pid, gameweek=gw, expected_points=pts,
                    expected_minutes=60.0, start_probability=0.8,
                    distribution=np.array([pts, pts, pts]),
                    floor=0.0, ceiling=pts * 2,
                )

            def get_player_prediction(self, player_id, gameweek):
                return self._pred(player_id, gameweek)

            def get_squad_predictions(self, squad_players, gws):
                return {gw: {pid: self._pred(pid, gw) for pid in squad_players} for gw in gws}

            def get_all_predictions(self, gameweek):
                return {pid: self._pred(pid, gameweek) for pid in pids}

            def get_fixture_count(self, player_id, gameweek):
                return 1

        # Baseline wrongly captains player 3 (actual points = 3); Phase 7
        # correctly captains player 4 (actual points = 4). The actual backtest
        # scores therefore differ.
        baseline = MiniProvider(captain_idx=2)
        phase7 = MiniProvider(captain_idx=3)

        result = evaluate_phase7(db_session, baseline, phase7, "2024-25")
        assert isinstance(result, Phase7EvaluationResult)
        assert result.season == "2024-25"
        # With only 1 gameweek present, both backtests run on GW1.
        assert result.baseline_total_points >= 0
        assert result.phase7_total_points >= 0
        # Results must be genuinely distinguishable (different captain -> different
        # actual points credited, not merely different predicted EV).
        assert result.phase7_total_points != result.baseline_total_points
        assert result.phase7_captain_delta is not None
"""
    content = prefix + block
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("REPLACED_BLOCK")
