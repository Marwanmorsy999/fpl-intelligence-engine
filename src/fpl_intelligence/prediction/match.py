"""Match prediction model (Poisson-style baseline).

Predicts for a fixture:

- expected home goals (lambda_home)
- expected away goals (lambda_away)
- home win probability
- draw probability
- away win probability
- clean-sheet probability for both teams

Assumptions (documented):

- Goals follow independent Poisson distributions. Independence between home
  and away goal counts is a simplifying assumption; real matches exhibit
  correlation (game state, tactics). This is acceptable for a baseline and
  is relaxed in the simulation layer which can model joint outcomes.
- Expected goals are derived from team strength estimates as of the cutoff.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fpl_intelligence.prediction.team import LEAGUE_AVG_GOALS, TeamStrengthEstimate


@dataclass
class MatchPrediction:
    """A probabilistic match prediction.

    Attributes:
        fixture_id: Fixture ID.
        cutoff_time: The decision cutoff.
        expected_home_goals: Expected home goals (lambda_home).
        expected_away_goals: Expected away goals (lambda_away).
        home_win_probability: P(home win).
        draw_probability: P(draw).
        away_win_probability: P(away win).
        home_clean_sheet_probability: P(home CS).
        away_clean_sheet_probability: P(away CS).
        home_attack_strength: Home attack strength used.
        away_attack_strength: Away attack strength used.
        home_defence_strength: Home defence strength used.
        away_defence_strength: Away defence strength used.
        max_goals: Maximum scoreline considered in the Poisson sum.
        scoreline_distribution: Optional computed scoreline probabilities.
    """

    fixture_id: int
    cutoff_time: datetime
    expected_home_goals: float
    expected_away_goals: float
    home_win_probability: float
    draw_probability: float
    away_win_probability: float
    home_clean_sheet_probability: float
    away_clean_sheet_probability: float
    home_attack_strength: float = 1.0
    away_attack_strength: float = 1.0
    home_defence_strength: float = 1.0
    away_defence_strength: float = 1.0
    max_goals: int = 8
    scoreline_distribution: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "cutoff_time": self.cutoff_time.isoformat(),
            "expected_home_goals": round(self.expected_home_goals, 4),
            "expected_away_goals": round(self.expected_away_goals, 4),
            "home_win_probability": round(self.home_win_probability, 4),
            "draw_probability": round(self.draw_probability, 4),
            "away_win_probability": round(self.away_win_probability, 4),
            "home_clean_sheet_probability": round(self.home_clean_sheet_probability, 4),
            "away_clean_sheet_probability": round(self.away_clean_sheet_probability, 4),
            "home_attack_strength": self.home_attack_strength,
            "away_attack_strength": self.away_attack_strength,
            "home_defence_strength": self.home_defence_strength,
            "away_defence_strength": self.away_defence_strength,
        }


class PoissonMatchModel:
    """A Poisson-style match prediction model.

    Expected goals for the home side:

    .. code-block:: text

        lambda_home = league_avg_goals
                      * home_attack_strength
                      * away_defence_strength
                      * home_advantage

    The ``home_advantage`` factor (default ~1.2) encodes the well-known
    home advantage effect. Similarly for the away side (no home advantage).
    """

    def __init__(
        self,
        feature_version: str = "1.0.0",
        league_avg_goals: float = LEAGUE_AVG_GOALS,
        home_advantage: float = 1.2,
        max_goals: int = 8,
    ) -> None:
        self._feature_version = feature_version
        self._league_avg_goals = league_avg_goals
        self._home_advantage = home_advantage
        self._max_goals = max_goals

    @property
    def model_name(self) -> str:
        return "match_prediction_model"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict_from_strengths(
        self,
        fixture_id: int,
        cutoff_time: datetime,
        home_strength: TeamStrengthEstimate | dict[str, float] | None = None,
        away_strength: TeamStrengthEstimate | dict[str, float] | None = None,
    ) -> MatchPrediction:
        """Predict a match from team strength estimates.

        Args:
            fixture_id: Fixture ID.
            cutoff_time: The decision cutoff.
            home_strength: Home team strength (estimate or dict).
            away_strength: Away team strength (estimate or dict).

        Returns:
            A ``MatchPrediction``.
        """
        home = self._strength_to_dict(home_strength)
        away = self._strength_to_dict(away_strength)

        home_attack = home.get("attack_strength", 1.0)
        away_defence = away.get("defence_strength", 1.0)
        away_attack = away.get("attack_strength", 1.0)
        home_defence = home.get("defence_strength", 1.0)

        lambda_home = self._league_avg_goals * home_attack * away_defence * self._home_advantage
        lambda_away = self._league_avg_goals * away_attack * home_defence

        # Clamp to a realistic range.
        lambda_home = max(0.1, min(5.0, lambda_home))
        lambda_away = max(0.1, min(5.0, lambda_away))

        probs = self._poisson_scoreline_probs(lambda_home, lambda_away)
        home_win = sum(p for (h, a), p in probs.items() if h > a)
        draw = sum(p for (h, a), p in probs.items() if h == a)
        away_win = sum(p for (h, a), p in probs.items() if h < a)
        home_cs = sum(p for (h, a), p in probs.items() if a == 0)
        away_cs = sum(p for (h, a), p in probs.items() if h == 0)

        return MatchPrediction(
            fixture_id=fixture_id,
            cutoff_time=cutoff_time,
            expected_home_goals=lambda_home,
            expected_away_goals=lambda_away,
            home_win_probability=home_win,
            draw_probability=draw,
            away_win_probability=away_win,
            home_clean_sheet_probability=home_cs,
            away_clean_sheet_probability=away_cs,
            home_attack_strength=home_attack,
            away_attack_strength=away_attack,
            home_defence_strength=home_defence,
            away_defence_strength=away_defence,
            max_goals=self._max_goals,
            scoreline_distribution={f"{h}-{a}": round(p, 6) for (h, a), p in probs.items()},
        )

    def predict_from_features(
        self,
        fixture_id: int,
        cutoff_time: datetime,
        home_features: dict[str, float],
        away_features: dict[str, float],
    ) -> MatchPrediction:
        """Predict a match from pre-computed team feature dicts."""
        home = {
            "attack_strength": home_features.get("attack_strength", 1.0),
            "defence_strength": home_features.get("defensive_strength", 1.0),
        }
        away = {
            "attack_strength": away_features.get("attack_strength", 1.0),
            "defence_strength": away_features.get("defensive_strength", 1.0),
        }
        return self.predict_from_strengths(fixture_id, cutoff_time, home, away)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _strength_to_dict(
        self, strength: TeamStrengthEstimate | dict[str, float] | None
    ) -> dict[str, float]:
        if strength is None:
            return {}
        if isinstance(strength, dict):
            return strength
        return strength.to_dict()

    def _poisson_scoreline_probs(
        self, lambda_home: float, lambda_away: float
    ) -> dict[tuple[int, int], float]:
        """Compute the joint scoreline probability matrix."""
        home_pmf = [self._poisson_pmf(k, lambda_home) for k in range(self._max_goals + 1)]
        away_pmf = [self._poisson_pmf(k, lambda_away) for k in range(self._max_goals + 1)]
        probs: dict[tuple[int, int], float] = {}
        for h in range(self._max_goals + 1):
            for a in range(self._max_goals + 1):
                probs[(h, a)] = home_pmf[h] * away_pmf[a]
        return probs

    @staticmethod
    def _poisson_pmf(k: int, lam: float) -> float:
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        return math.exp(-lam) * (lam**k) / math.factorial(k)


def normalize_probabilities(probs: dict[str, float]) -> dict[str, float]:
    """Normalize a dict of probabilities to sum to 1.0 (for tests/validation)."""
    total = sum(probs.values())
    if total <= 0:
        return probs
    return {k: v / total for k, v in probs.items()}
