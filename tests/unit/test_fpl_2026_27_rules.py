from pathlib import Path

import pytest

from fpl_intelligence.prediction.scoring import FPLPointsComponents, FPLScoringEngine


RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "fpl_rules" / "2026-27.yaml"


def test_2026_27_rules_file_loads_and_uses_official_goal_points() -> None:
    engine = FPLScoringEngine().load_rules_yaml(str(RULES_PATH))

    assert engine.rules_version == "2026-27-official"
    assert engine.compute(FPLPointsComponents(expected_goals=1.0), position_code=1)["goals"] == pytest.approx(10.0)
    assert engine.compute(FPLPointsComponents(expected_goals=1.0), position_code=2)["goals"] == pytest.approx(6.0)
    assert engine.compute(FPLPointsComponents(expected_goals=1.0), position_code=3)["goals"] == pytest.approx(5.0)
    assert engine.compute(FPLPointsComponents(expected_goals=1.0), position_code=4)["goals"] == pytest.approx(4.0)


def test_2026_27_rules_cover_chips_and_transfer_rollover() -> None:
    import yaml

    rules = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))

    assert rules["chips"]["sets"] == 2
    assert set(rules["chips"]["available"]) == {
        "wildcard",
        "free_hit",
        "triple_captain",
        "bench_boost",
    }
    assert rules["transfers"]["max_rollover_free_transfers"] == 5
    assert rules["lockdown"]["policy"] == "09:00 UK time on day after final match of gameweek"
