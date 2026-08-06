from pathlib import Path

content = '''"""Holdout policy for the FPL Intelligence Engine.

Explicit modes:
    development: Training, HP tuning, feature selection. No holdout data.
    validation: Temporal validation using development seasons only.
    final_holdout_evaluation: Read-only access to holdout data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

DEVELOPMENT_SEASONS = ["2022-23", "2023-24", "2024-25"]
FINAL_HOLDOUT_SEASONS = ["2025-26"]
VALIDATION_SEASONS = ["2022-23", "2023-24", "2024-25"]
ALL_SEASONS = DEVELOPMENT_SEASONS + FINAL_HOLDOUT_SEASONS
HOLDOUT_SEASON_CUTOFF = {"2025-26": datetime(2025, 8, 31)}


class HoldoutMode:
    DEVELOPMENT = "development"
    VALIDATION = "validation"
    FINAL_HOLDOUT_EVALUATION = "final_holdout_evaluation"

    @classmethod
    def all(cls):
        return [cls.DEVELOPMENT, cls.VALIDATION, cls.FINAL_HOLDOUT_EVALUATION]


@dataclass
class SeasonSplit:
    development_seasons: list = field(default_factory=lambda: list(DEVELOPMENT_SEASONS))
    validation_seasons: list = field(default_factory=lambda: list(VALIDATION_SEASONS))
    final_holdout_seasons: list = field(default_factory=lambda: list(FINAL_HOLDOUT_SEASONS))

    def is_holdout(self, season):
        return season in self.final_holdout_seasons

    def is_development(self, season):
        return season in self.development_seasons

    def is_validation(self, season):
        return season in self.validation_seasons

    def allowed_for_training(self, season, mode=HoldoutMode.DEVELOPMENT):
        if self.is_holdout(season):
            raise HoldoutViolationError(
                f"Season '{season}' is in the locked final holdout. "
                f"Mode '{mode}' does not allow holdout data."
            )
        return True

    def validate_training_seasons(self, seasons, mode=HoldoutMode.DEVELOPMENT):
        for season in seasons:
            self.allowed_for_training(season, mode=mode)

    def validate_observation(self, season, observation_date=None, mode=HoldoutMode.DEVELOPMENT):
        if not self.is_holdout(season):
            return
        if mode == HoldoutMode.FINAL_HOLDOUT_EVALUATION:
            return
        raise HoldoutViolationError(
            f"Observation from season '{season}' is in the locked final holdout. "
            f"Mode '{mode}' does not allow access."
        )

    def to_dict(self):
        return {
            "development_seasons": self.development_seasons,
            "validation_seasons": self.validation_seasons,
            "final_holdout_seasons": self.final_holdout_seasons,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            development_seasons=data.get("development_seasons", list(DEVELOPMENT_SEASONS)),
            validation_seasons=data.get("validation_seasons", list(VALIDATION_SEASONS)),
            final_holdout_seasons=data.get("final_holdout_seasons", list(FINAL_HOLDOUT_SEASONS)),
        )


class HoldoutViolationError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


DEFAULT_SEASON_SPLIT = SeasonSplit()


def get_default_split():
    return DEFAULT_SEASON_SPLIT


def enforce_holdout(season=None, seasons=None, target_date=None, cutoff_date=None, mode=HoldoutMode.DEVELOPMENT, split=None):
    if mode not in HoldoutMode.all():
        raise ValueError(f"Invalid mode '{mode}'. Must be one of {HoldoutMode.all()}")

    split = split or DEFAULT_SEASON_SPLIT
    all_seasons = []
    if season is not None:
        all_seasons.append(season)
    if seasons is not None:
        all_seasons.extend(seasons)

    for s in all_seasons:
        split.allowed_for_training(s, mode=mode)
        if split.is_holdout(s) and target_date is not None:
            holdout_cutoff = HOLDOUT_SEASON_CUTOFF.get(s)
            if holdout_cutoff and target_date >= holdout_cutoff:
                if mode != HoldoutMode.FINAL_HOLDOUT_EVALUATION:
                    raise HoldoutViolationError(
                        f"Target date {target_date.isoformat()} in season '{s}' "
                        f"is on or after holdout cutoff. Mode '{mode}' not allowed."
                    )

    if cutoff_date is not None and target_date is not None:
        for s in all_seasons:
            if split.is_holdout(s):
                holdout_cutoff = HOLDOUT_SEASON_CUTOFF.get(s)
                if holdout_cutoff and target_date >= holdout_cutoff:
                    if mode != HoldoutMode.FINAL_HOLDOUT_EVALUATION:
                        raise HoldoutViolationError(
                            f"Target date {target_date.isoformat()} is on or after "
                            f"holdout cutoff for season '{s}'. Mode '{mode}' not allowed."
                        )

    return {
        "season": season,
        "seasons": seasons,
        "mode": mode,
        "target_date": target_date.isoformat() if target_date else None,
        "cutoff_date": cutoff_date.isoformat() if cutoff_date else None,
        "allowed": True,
    }
'''

Path('src/fpl_intelligence/config/holdout.py').write_text(content)
print('Wrote holdout.py:', len(content), 'bytes')
