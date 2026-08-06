"""FPL scoring engine.

Converts expected statistical outcomes into expected FPL points using
season-specific scoring rules.

The scoring engine deliberately separates:

1. **Expected statistical outcomes** (goals, assists, clean sheet, bonus,
   appearance, defensive contributions)
2. **FPL scoring conversion** (points per unit, per season rules)

Scoring rules are versioned. The default rules encode the standard FPL
scoring constants. Season-specific differences are supported by passing a
different rules dict (e.g. loaded from ``config/fpl_rules/2026-27.yaml``).

The engine is testable independently and must NOT be embedded in the
prediction models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_RULES: dict[str, Any] = {
    "rules_version": "default-official",
    "points": {
        "goal": {"GK": 6, "DEF": 6, "MID": 5, "FWD": 4},
        "assist": 3,
        "clean_sheet": {"GK": 4, "DEF": 4, "MID": 1},
        "appearance_minutes_60_plus": 2,
        "appearance_minutes_under_60": 1,
        "penalty_save": 5,
        "penalty_miss": -2,
        "own_goal": -2,
        "yellow_card": -1,
        "red_card": -3,
        "goals_conceded_2_plus": {"GK": -1, "DEF": -1},
        "bonus": None,  # Bonus is model-estimated, not rule-derived.
    },
    "default_position": "MID",
}


@dataclass
class FPLPointsComponents:
    """Expected FPL point components (before aggregation).

    Attributes:
        expected_goals: Expected goals scored.
        expected_assists: Expected assists.
        expected_clean_sheet: Probability of a clean sheet (0-1).
        expected_bonus: Expected bonus points.
        appearance_minutes: Expected minutes (for appearance points).
        expected_penalty_saves: Expected penalty saves.
        expected_penalty_misses: Expected penalty misses.
        expected_own_goals: Expected own goals.
        expected_yellow_cards: Expected yellow cards.
        expected_red_cards: Expected red cards.
        expected_goals_conceded: Expected goals conceded.
        defensive_contribution: Expected defensive contribution points.
    """

    expected_goals: float = 0.0
    expected_assists: float = 0.0
    expected_clean_sheet: float = 0.0
    expected_bonus: float = 0.0
    appearance_minutes: float = 0.0
    expected_penalty_saves: float = 0.0
    expected_penalty_misses: float = 0.0
    expected_own_goals: float = 0.0
    expected_yellow_cards: float = 0.0
    expected_red_cards: float = 0.0
    expected_goals_conceded: float = 0.0
    defensive_contribution: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "expected_goals": self.expected_goals,
            "expected_assists": self.expected_assists,
            "expected_clean_sheet": self.expected_clean_sheet,
            "expected_bonus": self.expected_bonus,
            "appearance_minutes": self.appearance_minutes,
            "expected_penalty_saves": self.expected_penalty_saves,
            "expected_penalty_misses": self.expected_penalty_misses,
            "expected_own_goals": self.expected_own_goals,
            "expected_yellow_cards": self.expected_yellow_cards,
            "expected_red_cards": self.expected_red_cards,
            "expected_goals_conceded": self.expected_goals_conceded,
            "defensive_contribution": self.defensive_contribution,
        }


class FPLScoringEngine:
    """Converts expected statistical outcomes to expected FPL points.

    Args:
        rules: Optional rules dict. Defaults to ``DEFAULT_RULES``.
    """

    def __init__(self, rules: dict[str, Any] | None = None) -> None:
        self._rules = rules or DEFAULT_RULES
        self._points = self._rules["points"]

    @property
    def rules_version(self) -> str:
        return self._rules.get("rules_version", "unknown")

    # ------------------------------------------------------------------
    # Main conversion
    # ------------------------------------------------------------------

    def compute(
        self,
        components: FPLPointsComponents,
        position_code: int = 3,
    ) -> dict[str, float]:
        """Alias for expected_points. Used by simulation code."""
        return self.expected_points(components, position_code)

    def expected_points(
        self,
        components: FPLPointsComponents,
        position_code: int = 3,
    ) -> dict[str, float]:
        """Convert expected statistical components to expected FPL points.

        Args:
            components: Expected statistical outcomes.
            position_code: FPL position (1=GK, 2=DEF, 3=MID, 4=FWD).

        Returns:
            Dict with component point contributions and the total expected points.
        """
        position = self._position_name(position_code)

        goals_pts = components.expected_goals * self._goal_points(position)
        assists_pts = components.expected_assists * self._assist_points()
        cs_pts = components.expected_clean_sheet * self._clean_sheet_points(position)

        # Appearance points: 2 if >=60 min, 1 if <60 but >0.
        appearance_pts = self._appearance_points(components.appearance_minutes)

        bonus_pts = components.expected_bonus
        pen_save_pts = components.expected_penalty_saves * self._points.get("penalty_save", 5)
        pen_miss_pts = components.expected_penalty_misses * self._points.get("penalty_miss", -2)
        own_goal_pts = components.expected_own_goals * self._points.get("own_goal", -2)
        yellow_pts = components.expected_yellow_cards * self._points.get("yellow_card", -1)
        red_pts = components.expected_red_cards * self._points.get("red_card", -3)

        # Defensive deduction for GK/DEF conceding 2+ goals.
        conceded_pts = 0.0
        if position in ("GK", "DEF"):
            p = self._points.get("goals_conceded_2_plus", {})
            per_conceded = p.get(position, 0)
            # Expected deduction applies when expected goals conceded > 1.5
            # (approximate P(concede 2+) via a simple clip).
            p_2plus = min(1.0, max(0.0, components.expected_goals_conceded - 1.5))
            conceded_pts = per_conceded * p_2plus

        defensive_pts = components.defensive_contribution

        total = (
            goals_pts
            + assists_pts
            + cs_pts
            + appearance_pts
            + bonus_pts
            + pen_save_pts
            + pen_miss_pts
            + own_goal_pts
            + yellow_pts
            + red_pts
            + conceded_pts
            + defensive_pts
        )

        return {
            "total": round(total, 4),
            "goals": round(goals_pts, 4),
            "assists": round(assists_pts, 4),
            "clean_sheet": round(cs_pts, 4),
            "appearance": round(appearance_pts, 4),
            "bonus": round(bonus_pts, 4),
            "penalty_save": round(pen_save_pts, 4),
            "penalty_miss": round(pen_miss_pts, 4),
            "own_goal": round(own_goal_pts, 4),
            "yellow_card": round(yellow_pts, 4),
            "red_card": round(red_pts, 4),
            "goals_conceded_deduction": round(conceded_pts, 4),
            "defensive_contribution": round(defensive_pts, 4),
        }

    # ------------------------------------------------------------------
    # Season-specific rules
    # ------------------------------------------------------------------

    def with_rules(self, rules: dict[str, Any]) -> FPLScoringEngine:
        """Return a new scoring engine with different (versioned) rules."""
        return FPLScoringEngine(rules)

    def load_rules_yaml(self, path: str) -> FPLScoringEngine:
        """Load scoring rules from a YAML file (e.g. config/fpl_rules/*.yaml)."""
        import yaml  # type: ignore[import-untyped]

        with open(path, encoding="utf-8") as f:
            rules = yaml.safe_load(f)
        return self.with_rules(rules)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _goal_points(self, position: str) -> float:
        goals = self._points.get("goal", {})
        return float(goals.get(position, 4))

    def _assist_points(self) -> float:
        return float(self._points.get("assist", 3))

    def _clean_sheet_points(self, position: str) -> float:
        cs = self._points.get("clean_sheet", {})
        return float(cs.get(position, 0))

    def _appearance_points(self, minutes: float) -> float:
        """Appearance points based on expected minutes."""
        if minutes <= 0:
            return 0.0
        if minutes >= 60:
            return float(self._points.get("appearance_minutes_60_plus", 2))
        # Linear approximation for 0 < minutes < 60.
        base = float(self._points.get("appearance_minutes_under_60", 1))
        return base * (minutes / 60.0)

    def _position_name(self, position_code: int) -> str:
        mapping = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
        return mapping.get(position_code, self._rules.get("default_position", "MID"))

