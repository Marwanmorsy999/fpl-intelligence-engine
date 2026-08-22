"""Data-completeness and quality assessment for model predictions.

Every model prediction should expose an explainable data-quality score.

The score is decomposed into feature group completeness:

- player_history: historical match/gameweek performance data.
- team_history: team match performance data.
- fixture_data: fixture metadata (home/away, kickoff, etc.).
- market_data: FPL market snapshots (price, ownership).
- minutes_inputs: features used by the minutes model.

Each group is scored 0-1. A missing group is NOT automatically scored as 1
(complete) unless there is a documented reason. Scores are weighted to
produce an overall ``data_completeness`` in [0, 1].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Feature group definitions used by the data-completeness scorer.
FEATURE_GROUPS: dict[str, list[str]] = {
    "player_history": [
        "points_last_3",
        "points_last_5",
        "points_last_10",
        "goals_last_3",
        "assists_last_3",
    ],
    "team_history": [
        "attack_strength",
        "defensive_strength",
        "home_avg_goals",
        "away_avg_goals",
    ],
    "fixture_data": ["is_home", "fixture_difficulty", "days_of_rest"],
    "market_data": ["price", "ownership", "form", "selected_by_percent"],
    "minutes_inputs": [
        "minutes_last_3",
        "minutes_last_5",
        "minutes_last_10",
        "minutes_prev_match",
        "starts_last_3",
    ],
}


@dataclass
class DataQualityAssessment:
    """An explainable data-quality assessment.

    Attributes:
        overall: Combined completeness score (0-1).
        group_scores: Per-feature-group completeness (0-1).
        missing_groups: Groups that are entirely missing.
        explainability: Text explanation of the score.
        n_features_present: Number of features with a value.
        n_features_expected: Expected number of features.
    """

    overall: float = 0.0
    group_scores: dict[str, float] = field(default_factory=dict)
    missing_groups: list[str] = field(default_factory=list)
    explainability: str = ""
    n_features_present: int = 0
    n_features_expected: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "group_scores": self.group_scores,
            "missing_groups": self.missing_groups,
            "explainability": self.explainability,
            "n_features_present": self.n_features_present,
            "n_features_expected": self.n_features_expected,
        }


def assess_data_quality(
    features: dict[str, float],
    feature_groups: dict[str, list[str]] | None = None,
    missing_value_threshold: float = 0.0,
) -> DataQualityAssessment:
    """Assess data completeness for a feature vector.

    Args:
        features: The feature vector (dict of feature_name -> value).
        feature_groups: Dict of group_name -> list of expected feature keys.
            Defaults to ``FEATURE_GROUPS``.
        missing_value_threshold: Values at or below this threshold are
            considered "missing". Default 0.0 means only explicit zeros are
            treated as potentially missing, but presence is key-based.
            A value exactly equal to ``missing_value_threshold`` is still
            counted as present if the key exists.

    Returns:
        A ``DataQualityAssessment``.
    """
    groups = feature_groups or FEATURE_GROUPS
    feature_keys = set(features.keys())

    group_scores: dict[str, float] = {}
    missing_groups: list[str] = []
    total_present = 0
    total_expected = 0

    for group_name, expected_keys in groups.items():
        expected_set = set(expected_keys)
        present_keys = expected_set & feature_keys
        n_present = len(present_keys)
        n_expected = len(expected_set)

        if n_expected == 0:
            group_scores[group_name] = 1.0
            continue

        # Score: fraction of expected keys present.
        # Non-zero values are weighted higher.
        non_zero_present = sum(
            1 for k in present_keys if abs(features.get(k, 0.0)) > missing_value_threshold
        )
        presence_score = n_present / n_expected
        value_score = non_zero_present / n_expected
        group_score = 0.6 * presence_score + 0.4 * value_score

        group_scores[group_name] = round(group_score, 4)
        total_present += n_present
        total_expected += n_expected

        if group_score < 0.3:
            missing_groups.append(group_name)

    # Overall: weighted average across groups.
    # Player history and minutes inputs double-weighted for minutes model.
    weights = {
        "player_history": 2.0,
        "team_history": 1.5,
        "fixture_data": 1.0,
        "market_data": 1.0,
        "minutes_inputs": 2.0,
    }
    weight_sum = 0.0
    weighted_score = 0.0
    for group_name, score in group_scores.items():
        w = weights.get(group_name, 1.0)
        weighted_score += w * score
        weight_sum += w
    overall = round(weighted_score / weight_sum if weight_sum > 0 else 0.0, 4)

    # Explanation.
    if overall >= 0.9:
        explain = "High data completeness. All major feature groups are well-populated."
    elif overall >= 0.7:
        explain = f"Moderate completeness. Missing groups: {', '.join(missing_groups) or 'none'}"
    elif overall >= 0.4:
        explain = f"Low completeness. Significant gaps in: {', '.join(missing_groups) or 'unknown'}"
    else:
        explain = "Very low data completeness. Most feature groups are missing."

    return DataQualityAssessment(
        overall=overall,
        group_scores=group_scores,
        missing_groups=missing_groups,
        explainability=explain,
        n_features_present=total_present,
        n_features_expected=total_expected,
    )
