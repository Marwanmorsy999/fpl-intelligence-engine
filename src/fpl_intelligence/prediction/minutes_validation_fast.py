"""Performance-optimized strict walk-forward minutes validation.

This module keeps the existing validation semantics but removes two avoidable
costs in the original evaluator:

1. Historical performance rows are loaded once and filtered in memory for each
   chronological cutoff instead of issuing a large ORM query for every fold.
2. The inner blend model for fold k reuses the already-fitted outer model from
   fold k-1, because both are trained on exactly the same chronological prefix.

No model, feature, cutoff, or scoring rule is changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from fpl_intelligence.backtesting.cutoff import DecisionCutoff, get_all_gameweek_cutoffs
from fpl_intelligence.config.holdout import HoldoutMode, enforce_holdout
from fpl_intelligence.db.models import Gameweek, Player, PlayerGameweekPerformance, Season
from fpl_intelligence.features.temporal import DEFAULT_POLICY, InformationAccessPolicy
from fpl_intelligence.prediction.minutes import (
    MinutesModel,
    RecentStartBaseline,
    RollingAverageMinutesBaseline,
    SimpleRecentMinutesBaseline,
)
from fpl_intelligence.prediction.training import TrainingDataset
from fpl_intelligence.prediction.minutes_validation import (
    BLEND_WEIGHTS,
    MIN_TRAIN_ROWS,
    ValidationResult,
    ValidationRow,
    _position_name,
    blend_prediction,
    select_blend_weight,
)


@dataclass(frozen=True)
class _PerfRow:
    """Minimal immutable projection of the historical performance table."""

    player_id: int
    gameweek_id: int
    minutes: float | None
    total_points: float | None
    goals_scored: float | None
    assists: float | None
    available_at: datetime | None
    ingested_at: datetime | None


class FastMinutesWalkForwardEvaluator:
    """Drop-in evaluator with cached historical reads and model reuse."""

    def __init__(
        self,
        db,
        policy: InformationAccessPolicy = DEFAULT_POLICY,
        feature_version: str = "2.0.0",
        min_train_rows: int = MIN_TRAIN_ROWS,
    ) -> None:
        self.db = db
        self.policy = policy
        self.feature_version = feature_version
        self.min_train_rows = min_train_rows

    def run(self, seasons: list[str], initial_train_folds: int = 3) -> ValidationResult:
        if not seasons:
            return ValidationResult([], [], {"no_temporal_provenance": 0}, [])
        enforce_holdout(seasons=seasons, mode=HoldoutMode.DEVELOPMENT)

        cutoffs: list[DecisionCutoff] = []
        for season in seasons:
            cutoffs.extend(get_all_gameweek_cutoffs(self.db, season, policy=self.policy))
        cutoffs.sort(key=lambda cutoff: cutoff.cutoff_time)

        if not cutoffs:
            return ValidationResult(
                [], [], {"no_temporal_provenance": 0, "insufficient_training_rows": 0}, sorted(set(seasons))
            )

        datasets = self._build_datasets_once(cutoffs)
        position_by_player = dict(self.db.execute(select(Player.id, Player.position_code)).all())
        rows: list[ValidationRow] = []
        folds: list[dict[str, object]] = []
        exclusions = {
            "no_temporal_provenance": sum(
                len(dataset.targets) - len(dataset.features) for _, dataset in datasets
            ),
            "insufficient_training_rows": 0,
        }

        print(
            f"  Built {len(datasets)} datasets with cached historical reads; "
            f"{exclusions['no_temporal_provenance']} rows lack temporal provenance. "
            f"Evaluating folds (first {initial_train_folds} reserved for training)...",
            flush=True,
        )

        previous_outer_model: MinutesModel | None = None
        for fold_index, (cutoff, dataset) in enumerate(datasets):
            if fold_index < initial_train_folds:
                exclusions["insufficient_training_rows"] += len(dataset.targets)
                continue

            print(
                f"  [fold] {fold_index + 1}/{len(datasets)}: "
                f"{cutoff.season} GW{cutoff.gameweek} "
                f"-> {len(dataset.features)} eval players",
                flush=True,
            )

            train_datasets = datasets[:fold_index]
            train_rows = sum(len(train_dataset.entity_ids()) for _, train_dataset in train_datasets)
            if train_rows < self.min_train_rows:
                exclusions["insufficient_training_rows"] += len(dataset.targets)
                continue

            # Outer fit: identical training data to the original evaluator.
            model = self._fit_model(train_datasets)
            features = dataset.features
            player_ids = sorted(features)
            candidate_predictions = model.predict(
                [features[player_id] for player_id in player_ids]
            )
            candidate = {
                player_id: prediction
                for player_id, prediction in zip(player_ids, candidate_predictions, strict=True)
            }

            baseline_predictions = {
                "recent_minutes": SimpleRecentMinutesBaseline().predict_batch(features, cutoff),
                "recent_start": RecentStartBaseline().predict_batch(features, cutoff),
                "rolling_average": RollingAverageMinutesBaseline().predict_batch(features, cutoff),
            }

            weight, inner_training_window, inner_validation_window = self._inner_weight_cached(
                datasets, fold_index, previous_outer_model
            )
            blend = {
                player_id: blend_prediction(
                    candidate[player_id],
                    baseline_predictions["recent_minutes"][player_id],
                    weight,
                    inner_training_window,
                    inner_validation_window,
                )
                for player_id in player_ids
            }
            predictions = {
                "candidate": candidate,
                "blend": blend,
                **baseline_predictions,
            }

            for player_id in dataset.entity_ids():
                rows.append(
                    ValidationRow(
                        season=cutoff.season,
                        gameweek=cutoff.gameweek,
                        cutoff_time=cutoff.cutoff_time,
                        player_id=player_id,
                        position=_position_name(position_by_player.get(player_id)),
                        minutes=float(dataset.targets[player_id]),
                        predictions={
                            name: {player_id: values[player_id]}
                            for name, values in predictions.items()
                        },
                        features=features[player_id],
                    )
                )

            folds.append(
                {
                    "season": cutoff.season,
                    "gameweek": cutoff.gameweek,
                    "train_cutoff": datasets[fold_index - 1][0].cutoff_time.isoformat(),
                    "evaluation_cutoff": cutoff.cutoff_time.isoformat(),
                    "n_train_rows": train_rows,
                    "n_predictions": len(dataset.targets),
                    "blend_weight": weight,
                    "blend_inner_training_window": inner_training_window,
                    "blend_inner_validation_window": inner_validation_window,
                }
            )

            # This model is exactly the one needed as the next fold's inner model.
            previous_outer_model = model

        return ValidationResult(rows, folds, exclusions, sorted(set(seasons)))

    def _build_datasets_once(
        self, cutoffs: list[DecisionCutoff]
    ) -> list[tuple[DecisionCutoff, TrainingDataset]]:
        """Build every chronological dataset from two bounded DB reads."""
        max_cutoff = max(cutoff.cutoff_time for cutoff in cutoffs)
        cutoff_keys = {(cutoff.season, cutoff.gameweek) for cutoff in cutoffs}

        # Historical feature source: only rows whose required timestamps are
        # both known and no later than the final validation cutoff can ever be
        # eligible. Earlier folds apply their own stricter cutoff in memory.
        feature_stmt = (
            select(
                PlayerGameweekPerformance.player_id,
                PlayerGameweekPerformance.gameweek_id,
                PlayerGameweekPerformance.minutes,
                PlayerGameweekPerformance.total_points,
                PlayerGameweekPerformance.goals_scored,
                PlayerGameweekPerformance.assists,
                PlayerGameweekPerformance.available_at,
                PlayerGameweekPerformance.ingested_at,
            )
            .where(
                PlayerGameweekPerformance.available_at.is_not(None),
                PlayerGameweekPerformance.ingested_at.is_not(None),
                PlayerGameweekPerformance.available_at <= max_cutoff,
                PlayerGameweekPerformance.ingested_at <= max_cutoff,
            )
            .order_by(
                PlayerGameweekPerformance.player_id,
                PlayerGameweekPerformance.gameweek_id,
            )
        )
        feature_rows = [
            _PerfRow(
                player_id=int(player_id),
                gameweek_id=int(gameweek_id),
                minutes=minutes,
                total_points=total_points,
                goals_scored=goals_scored,
                assists=assists,
                available_at=available_at,
                ingested_at=ingested_at,
            )
            for (
                player_id,
                gameweek_id,
                minutes,
                total_points,
                goals_scored,
                assists,
                available_at,
                ingested_at,
            ) in self.db.execute(feature_stmt).all()
        ]
        histories: dict[int, list[_PerfRow]] = {}
        for row in feature_rows:
            histories.setdefault(row.player_id, []).append(row)

        # Outcome source: exact target gameweeks only. This intentionally reads
        # post-cutoff outcomes because those are the labels being scored.
        target_stmt = (
            select(
                PlayerGameweekPerformance.player_id,
                PlayerGameweekPerformance.gameweek_id,
                PlayerGameweekPerformance.minutes,
                Gameweek.provider_event_id,
                Season.code,
            )
            .join(Gameweek, Gameweek.id == PlayerGameweekPerformance.gameweek_id)
            .join(Season, Season.id == Gameweek.season_id)
            .where(
                Season.code.in_([cutoff.season for cutoff in cutoffs]),
                Gameweek.provider_event_id.in_([cutoff.gameweek for cutoff in cutoffs]),
            )
        )
        targets_by_key: dict[tuple[str, int], list[tuple[int, float]]] = {}
        for player_id, _gameweek_id, minutes, gameweek, season in self.db.execute(target_stmt).all():
            key = (str(season), int(gameweek))
            if key not in cutoff_keys:
                continue
            if player_id is None or minutes is None:
                continue
            targets_by_key.setdefault(key, []).append((int(player_id), float(minutes)))

        datasets: list[tuple[DecisionCutoff, TrainingDataset]] = []
        for cutoff in cutoffs:
            key = (cutoff.season, cutoff.gameweek)
            features: dict[int, dict[str, float]] = {}
            targets: dict[int, float] = {}
            for player_id, target_minutes in targets_by_key.get(key, []):
                eligible = [
                    row
                    for row in histories.get(player_id, [])
                    if row.available_at is not None
                    and row.ingested_at is not None
                    and row.available_at <= cutoff.cutoff_time
                    and row.ingested_at <= cutoff.cutoff_time
                ]
                if not eligible:
                    continue
                feature_row = {
                    **self._feature_builder(eligible),
                }
                features[player_id] = feature_row
                targets[player_id] = target_minutes
            datasets.append(
                (
                    cutoff,
                    TrainingDataset(
                        entity_type="player",
                        target="minutes",
                        feature_version=self.feature_version,
                        features=features,
                        targets=targets,
                        cutoff_time=cutoff.cutoff_time,
                        metadata={
                            "target_gameweek": cutoff.gameweek,
                            "policy": self.policy.value,
                        },
                    ),
                )
            )
            print(
                f"  [data] {len(datasets)}/{len(cutoffs)}: "
                f"{cutoff.season} GW{cutoff.gameweek} -> "
                f"{len(features)} features, {len(targets)} targets",
                flush=True,
            )
        return datasets

    @staticmethod
    def _feature_builder(perfs: list[_PerfRow]) -> dict[str, float]:
        """Exact extraction of TrainingDataBuilder player features."""
        features: dict[str, float] = {}
        for window in (3, 5, 10):
            recent = perfs[-window:]
            features[f"minutes_last_{window}"] = float(sum(p.minutes or 0 for p in recent))
            features[f"starts_last_{window}"] = float(
                sum(1 for p in recent if (p.minutes or 0) >= 60)
            )
            features[f"points_last_{window}"] = float(sum(p.total_points or 0 for p in recent))
            features[f"goals_last_{window}"] = float(sum(p.goals_scored or 0 for p in recent))
            features[f"assists_last_{window}"] = float(sum(p.assists or 0 for p in recent))
        last = perfs[-1]
        features["minutes_prev_match"] = float(last.minutes or 0)
        features["points_prev_match"] = float(last.total_points or 0)
        features["n_season_matches"] = float(len(perfs))
        total_minutes = sum(p.minutes or 0 for p in perfs[-10:])
        total_points = sum(p.total_points or 0 for p in perfs[-10:])
        features["points_per_90"] = (
            round(total_points / total_minutes * 90, 4) if total_minutes > 0 else 0.0
        )
        return features

    @staticmethod
    def _fit_model(train_datasets: list[tuple[DecisionCutoff, TrainingDataset]]) -> MinutesModel:
        train_features: dict[int, dict[str, float]] = {}
        train_targets: dict[int, float] = {}
        for _, train_dataset in train_datasets:
            for player_id in train_dataset.entity_ids():
                key = len(train_features)
                train_features[key] = train_dataset.features[player_id]
                train_targets[key] = train_dataset.targets[player_id]
        model = MinutesModel(feature_version="2.0.0")
        model.fit(train_features, train_targets, {"target": "minutes"})
        return model

    def _inner_weight_cached(
        self,
        datasets: list[tuple[DecisionCutoff, TrainingDataset]],
        fold_index: int,
        previous_outer_model: MinutesModel | None,
    ) -> tuple[float, str, str]:
        """Compute the exact inner blend selection, reusing the previous fold fit."""
        if fold_index < 2:
            return 0.0, "none", "none"
        inner_training = datasets[: fold_index - 1]
        inner_cutoff, inner_dataset = datasets[fold_index - 1]
        if sum(len(dataset.targets) for _, dataset in inner_training) < self.min_train_rows:
            return 0.0, "none", inner_cutoff.cutoff_time.isoformat()

        if previous_outer_model is None:
            inner_model = self._fit_model(inner_training)
        else:
            inner_model = previous_outer_model

        player_ids = sorted(inner_dataset.features)
        candidate = inner_model.predict(
            [inner_dataset.features[player_id] for player_id in player_ids]
        )
        recent = SimpleRecentMinutesBaseline().predict_batch(inner_dataset.features, inner_cutoff)
        weight = select_blend_weight(
            [prediction["expected_minutes"] for prediction in candidate],
            [recent[player_id]["expected_minutes"] for player_id in player_ids],
            [inner_dataset.targets[player_id] for player_id in player_ids],
            BLEND_WEIGHTS,
        )
        return (
            weight,
            inner_training[0][0].cutoff_time.isoformat(),
            inner_cutoff.cutoff_time.isoformat(),
        )
