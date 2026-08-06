from pathlib import Path

lines = []
lines.append('"""Phase 5 complete tests."""')
lines.append('from __future__ import annotations')
lines.append('')
lines.append('import os')
lines.append('import sys')
lines.append('')
lines.append('sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))')
lines.append('')
lines.append('import pytest')
lines.append('import numpy as np')
lines.append('from datetime import datetime, UTC')
lines.append('')
lines.append('from fpl_intelligence.config.holdout import (')
lines.append('    enforce_holdout, HoldoutMode, HoldoutViolationError, SeasonSplit,')
lines.append('    DEVELOPMENT_SEASONS, FINAL_HOLDOUT_SEASONS,')
lines.append(')')
lines.append('from fpl_intelligence.prediction.scoring import FPLPointsComponents, FPLScoringEngine')
lines.append('from fpl_intelligence.prediction.distributions.engine import DistributionEngine, PointDistribution')
lines.append('from fpl_intelligence.prediction.distributions.calibration import evaluate_calibration, CalibrationReport')
lines.append('from fpl_intelligence.prediction.advanced_player.goal_model import GoalModel, GoalPrediction')
lines.append('from fpl_intelligence.prediction.advanced_player.assist_model import AssistModel, AssistPrediction')
lines.append('from fpl_intelligence.prediction.advanced_player.clean_sheet_model import CleanSheetModel, CleanSheetPrediction')
lines.append('from fpl_intelligence.prediction.advanced_player.bonus_model import BonusModel, BonusPrediction')
lines.append('from fpl_intelligence.prediction.advanced_player.defensive_contribution_model import DefensiveContributionModel, DefensiveContributionPrediction')
lines.append('from fpl_intelligence.prediction.advanced_player.player_model import AdvancedPlayerModel')
lines.append('from fpl_intelligence.prediction.simulation import MatchSimulator, SimulationResult, GameweekSimulator, GameweekSimulationResult')
lines.append('from fpl_intelligence.simulation.joint import JointSimulator')
lines.append('from fpl_intelligence.simulation.gameweek import AdvancedGameweekSimulator')
lines.append('from fpl_intelligence.prediction.match import PoissonMatchModel, MatchPrediction')
lines.append('from fpl_intelligence.prediction.team import TeamStrengthEstimate')
lines.append('from fpl_intelligence.prediction.phase5_comparison import Phase5Comparison, ComparisonResult')
lines.append('')

header = chr(10).join(lines) + chr(10)*2

with open('tests/prediction/test_phase5_complete.py') as f:
    existing = f.read()

idx = existing.find('class TestHoldoutPolicy')
clean = existing[idx:] if idx >= 0 else existing

new_content = header + clean
Path('tests/prediction/test_phase5_complete.py').write_text(new_content)
print('Fixed, lines:', new_content.count(chr(10)))
