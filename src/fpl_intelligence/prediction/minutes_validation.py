"""Strict canonical walk-forward validation for the existing MinutesModel."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.backtesting.cutoff import DecisionCutoff, get_all_gameweek_cutoffs
from fpl_intelligence.config.holdout import HoldoutMode, enforce_holdout
from fpl_intelligence.db.models import Player
from fpl_intelligence.features.temporal import DEFAULT_POLICY, InformationAccessPolicy
from fpl_intelligence.prediction.minutes import (
    MinutesModel,
    RecentStartBaseline,
    RollingAverageMinutesBaseline,
    SimpleRecentMinutesBaseline,
)
from fpl_intelligence.prediction.training import TrainingDataBuilder

MODEL_VERSION = "2.0.0"
FEATURE_VERSION = "2.0.0"
DATA_VERSION = "canonical_historical_performance"
MIN_TRAIN_ROWS = 5
BLEND_WEIGHTS = tuple(round(index / 10, 1) for index in range(11))
MIN_CONDITIONAL_ROWS = 20
PROBABILITY_TARGETS = {
    "start": ("probability_start", lambda minutes: minutes >= 60),
    "appearance": ("probability_appearance", lambda minutes: minutes > 0),
    "60_plus": ("probability_60_plus", lambda minutes: minutes >= 60),
}


@dataclass(frozen=True)
class ValidationRow:
    season: str
    gameweek: int
    cutoff_time: datetime
    player_id: int
    position: str
    minutes: float
    predictions: dict[str, dict[int, dict[str, Any]]]
    features: dict[str, float]


@dataclass
class ValidationResult:
    rows: list[ValidationRow]
    folds: list[dict[str, Any]]
    exclusions: dict[str, int]
    seasons: list[str]
    data_error: str | None = None


def select_blend_weight(
    model_minutes: list[float] | np.ndarray,
    recent_minutes: list[float] | np.ndarray,
    actual_minutes: list[float] | np.ndarray,
    weights: tuple[float, ...] = BLEND_WEIGHTS,
) -> float:
    """Select the lowest-MAE blend weight from data available at inner validation."""
    model = np.asarray(model_minutes, dtype=float)
    recent = np.asarray(recent_minutes, dtype=float)
    actual = np.asarray(actual_minutes, dtype=float)
    if not (len(model) == len(recent) == len(actual)) or not len(actual):
        return 0.0
    scores = {
        weight: float(np.mean(np.abs(weight * model + (1.0 - weight) * recent - actual)))
        for weight in weights
    }
    return min(scores, key=lambda weight: (scores[weight], weight))


def select_conditional_blend_weights(
    model_minutes: list[float] | np.ndarray,
    recent_minutes: list[float] | np.ndarray,
    actual_minutes: list[float] | np.ndarray,
    groups: list[str],
    weights: tuple[float, ...] = BLEND_WEIGHTS,
    min_group_rows: int = MIN_CONDITIONAL_ROWS,
) -> dict[str, float]:
    """Return conditional weights only when every eligible group improves globally."""
    global_weight = select_blend_weight(model_minutes, recent_minutes, actual_minutes, weights)
    model = np.asarray(model_minutes, dtype=float)
    recent = np.asarray(recent_minutes, dtype=float)
    actual = np.asarray(actual_minutes, dtype=float)
    if not (len(model) == len(recent) == len(actual) == len(groups)):
        return {"global": global_weight}
    selected = {"global": global_weight}
    global_error = (
        float(np.mean(np.abs(global_weight * model + (1.0 - global_weight) * recent - actual)))
        if len(actual)
        else float("inf")
    )
    for group in sorted(set(groups)):
        mask = np.array([value == group for value in groups], dtype=bool)
        if int(mask.sum()) < min_group_rows:
            continue
        group_weight = select_blend_weight(model[mask], recent[mask], actual[mask], weights)
        group_error = float(
            np.mean(
                np.abs(
                    group_weight * model[mask]
                    + (1.0 - group_weight) * recent[mask]
                    - actual[mask]
                )
            )
        )
        group_global_error = float(
            np.mean(
                np.abs(
                    global_weight * model[mask]
                    + (1.0 - global_weight) * recent[mask]
                    - actual[mask]
                )
            )
        )
        if group_error < group_global_error and group_error < global_error:
            selected[group] = group_weight
    return selected


def blend_prediction(
    model_prediction: dict[str, Any],
    recent_prediction: dict[str, Any],
    weight: float,
    training_window: str,
    validation_window: str,
) -> dict[str, Any]:
    """Copy a model prediction and blend only its scalar expected-minutes field."""
    result = dict(model_prediction)
    result["expected_minutes"] = round(
        min(
            90.0,
            max(
                0.0,
                weight * float(model_prediction["expected_minutes"])
                + (1.0 - weight) * float(recent_prediction["expected_minutes"]),
            ),
        ),
        6,
    )
    result.update(
        {
            "expected_minutes_method": "walkforward_blend",
            "expected_minutes_model_weight": round(weight, 1),
            "expected_minutes_feature_version": FEATURE_VERSION,
            "expected_minutes_model_version": MODEL_VERSION,
            "expected_minutes_training_window": training_window,
            "expected_minutes_validation_window": validation_window,
        }
    )
    return result


class MinutesWalkForwardEvaluator:
    """Evaluate MinutesModel and fixed baselines in chronological order."""

    def __init__(
        self,
        db: Session,
        policy: InformationAccessPolicy = DEFAULT_POLICY,
        feature_version: str = FEATURE_VERSION,
        min_train_rows: int = MIN_TRAIN_ROWS,
    ) -> None:
        self.db = db
        self.policy = policy
        self.feature_version = feature_version
        self.min_train_rows = min_train_rows
        self.builder = TrainingDataBuilder(db, policy)

    def run(self, seasons: list[str], initial_train_folds: int = 3) -> ValidationResult:
        if not seasons:
            return ValidationResult([], [], {"no_temporal_provenance": 0}, [])
        enforce_holdout(seasons=seasons, mode=HoldoutMode.DEVELOPMENT)

        cutoffs: list[DecisionCutoff] = []
        for season in seasons:
            cutoffs.extend(get_all_gameweek_cutoffs(self.db, season, policy=self.policy))
        cutoffs.sort(key=lambda cutoff: cutoff.cutoff_time)

        datasets = []
        for idx, cutoff in enumerate(cutoffs):
            dataset = self.builder.build_player_dataset(
                "minutes", cutoff.cutoff_time, self.feature_version
            )
            datasets.append((cutoff, dataset))
            print(
                f"  [data] {idx + 1}/{len(cutoffs)}: "
                f"{cutoff.season} GW{cutoff.gameweek} "
                f"-> {len(dataset.features)} features, "
                f"{len(dataset.targets)} targets",
                flush=True,
            )
        position_by_player = dict(self.db.execute(select(Player.id, Player.position_code)).all())
        rows: list[ValidationRow] = []
        folds: list[dict[str, Any]] = []
        exclusions = {"no_temporal_provenance": 0, "insufficient_training_rows": 0}
        exclusions["no_temporal_provenance"] = sum(
            len(dataset.targets) - len(dataset.features) for _, dataset in datasets
        )

        print(
            f"  Built {len(datasets)} datasets; "
            f"{exclusions['no_temporal_provenance']} rows lack temporal provenance. "
            f"Evaluating folds (first {initial_train_folds} reserved for training)...",
            flush=True,
        )
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
            train_rows = [
                row for _, train_dataset in train_datasets for row in train_dataset.entity_ids()
            ]
            if len(train_rows) < self.min_train_rows:
                exclusions["insufficient_training_rows"] += len(dataset.targets)
                continue

            model = self._fit_model(train_datasets)
            features = dataset.features
            player_ids = sorted(features)
            candidate = {
                player_id: prediction
                for player_id, prediction in zip(
                    player_ids,
                    model.predict([features[player_id] for player_id in player_ids]),
                    strict=True,
                )
            }
            baseline_predictions = {
                "recent_minutes": SimpleRecentMinutesBaseline().predict_batch(features, cutoff),
                "recent_start": RecentStartBaseline().predict_batch(features, cutoff),
                "rolling_average": RollingAverageMinutesBaseline().predict_batch(features, cutoff),
            }
            weight, inner_training_window, inner_validation_window = self._inner_weight(
                train_datasets
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
            predictions: dict[str, dict[int, dict[str, Any]]] = {
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
                    "n_train_rows": len(train_rows),
                    "n_predictions": len(dataset.targets),
                    "blend_weight": weight,
                    "blend_inner_training_window": inner_training_window,
                    "blend_inner_validation_window": inner_validation_window,
                }
            )
        return ValidationResult(rows, folds, exclusions, sorted(set(seasons)))

    def _fit_model(self, train_datasets: list[Any]) -> MinutesModel:
        train_features: dict[int, dict[str, float]] = {}
        train_targets: dict[int, float] = {}
        for _, train_dataset in train_datasets:
            for player_id in train_dataset.entity_ids():
                train_features[len(train_features)] = train_dataset.features[player_id]
                train_targets[len(train_targets)] = train_dataset.targets[player_id]
        model = MinutesModel(feature_version=self.feature_version)
        model.fit(train_features, train_targets, {"target": "minutes"})
        return model

    def _inner_weight(
        self, train_datasets: list[tuple[DecisionCutoff, Any]]
    ) -> tuple[float, str, str]:
        """Choose a weight using only a final inner period within the outer train window."""
        if len(train_datasets) < 2:
            return 0.0, "none", "none"
        inner_training = train_datasets[:-1]
        inner_cutoff, inner_dataset = train_datasets[-1]
        if sum(len(dataset.targets) for _, dataset in inner_training) < self.min_train_rows:
            return 0.0, "none", inner_cutoff.cutoff_time.isoformat()
        model = self._fit_model(inner_training)
        player_ids = sorted(inner_dataset.features)
        candidate = model.predict([inner_dataset.features[player_id] for player_id in player_ids])
        recent = SimpleRecentMinutesBaseline().predict_batch(inner_dataset.features, inner_cutoff)
        weight = select_blend_weight(
            [prediction["expected_minutes"] for prediction in candidate],
            [recent[player_id]["expected_minutes"] for player_id in player_ids],
            [inner_dataset.targets[player_id] for player_id in player_ids],
        )
        return (
            weight,
            inner_training[0][0].cutoff_time.isoformat(),
            inner_cutoff.cutoff_time.isoformat(),
        )


def metric_summary(rows: list[ValidationRow], model_name: str) -> dict[str, float | int | None]:
    n = len(rows)
    if not n:
        return {
            "n": 0,
            "mae": None,
            "rmse": None,
            "start_brier": None,
            "start_log_loss": None,
            "start_calibration_error": None,
            "appearance_brier": None,
            "appearance_log_loss": None,
            "sixty_plus_brier": None,
            "sixty_plus_log_loss": None,
            "accuracy_start": None,
            "precision_start": None,
            "recall_start": None,
            "roc_auc_start": None,
        }
    actual_minutes = np.array([row.minutes for row in rows], dtype=float)
    expected = np.array(
        [row.predictions[model_name][row.player_id]["expected_minutes"] for row in rows]
    )
    summary: dict[str, float | int | None] = {
        "n": n,
        "mae": float(np.mean(np.abs(expected - actual_minutes))),
        "rmse": float(np.sqrt(np.mean((expected - actual_minutes) ** 2))),
    }
    for target, (key, outcome) in PROBABILITY_TARGETS.items():
        metric_target = "sixty_plus" if target == "60_plus" else target
        probabilities = np.array(
            [row.predictions[model_name][row.player_id].get(key, 0.0) for row in rows]
        )
        actual = np.array([float(outcome(row.minutes)) for row in rows])
        summary[f"{metric_target}_brier"] = float(np.mean((probabilities - actual) ** 2))
        summary[f"{metric_target}_log_loss"] = _log_loss(probabilities, actual)
        if target == "start":
            predicted = probabilities >= 0.5
            summary["accuracy_start"] = float(np.mean(predicted == actual))
            true_positives = float(np.sum(predicted & (actual == 1)))
            summary["precision_start"] = _ratio(true_positives, float(np.sum(predicted)))
            summary["recall_start"] = _ratio(true_positives, float(np.sum(actual)))
            summary["roc_auc_start"] = _roc_auc(probabilities, actual)
            summary["start_calibration_error"] = calibration_error(probabilities, actual)
    return summary


def reliability_table(
    rows: list[ValidationRow], model_name: str, target: str
) -> list[dict[str, float | int | None]]:
    key, outcome = PROBABILITY_TARGETS[target]
    table = []
    for bucket in range(10):
        selected = []
        for row in rows:
            probability = float(row.predictions[model_name][row.player_id].get(key, 0.0))
            if bucket == 9:
                in_bucket = bucket / 10 <= probability <= 1.0
            else:
                in_bucket = bucket / 10 <= probability < (bucket + 1) / 10
            if in_bucket:
                selected.append((probability, float(outcome(row.minutes))))
        table.append(
            {
                "bucket": f"{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}",
                "predicted_probability": _mean([item[0] for item in selected]),
                "observed_frequency": _mean([item[1] for item in selected]),
                "n": len(selected),
            }
        )
    return table


def build_breakdown(
    rows: list[ValidationRow], model_names: list[str], field: str
) -> dict[str, dict[str, dict[str, float | int | None]]]:
    groups: dict[str, list[ValidationRow]] = defaultdict(list)
    for row in rows:
        if field == "season":
            group = row.season
        elif field == "position":
            group = row.position
        elif field == "minutes_tier":
            group = _minutes_tier(row.minutes)
        else:
            group = _frequency_group(row.features)
        groups[group].append(row)
    return {
        group: {model: metric_summary(group_rows, model) for model in model_names}
        for group, group_rows in sorted(groups.items())
    }


def render_report(result: ValidationResult) -> str:
    models = ["recent_minutes", "recent_start", "rolling_average", "candidate", "blend"]
    overall = {model: metric_summary(result.rows, model) for model in models}
    candidate = overall["candidate"]
    baseline_mae = min(
        (float(overall[name]["mae"]) for name in models[:3] if overall[name]["mae"] is not None),
        default=None,
    )
    promoted = bool(
        candidate["n"]
        and candidate["mae"] is not None
        and baseline_mae is not None
        and float(candidate["mae"]) < baseline_mae
        and float(candidate["start_brier"] or 1)
        < min(float(overall[name]["start_brier"] or 1) for name in models[:3])
    )
    decision = (
        "PROMOTE"
        if promoted
        else "KEEP AS CANDIDATE"
        if candidate["n"]
        else "INSUFFICIENT EVIDENCE"
    )

    lines = [
        "# Stage 2A Minutes Validation",
        "",
        "## Executive result",
        f"Candidate rows: N = {candidate['n']}. Promotion decision: **{decision}**.",
        "The candidate is promoted only when it wins both expected-minutes MAE and start Brier "
        "score against every required baseline; this report does not tune the model.",
        "",
        "## Data",
        f"Dataset: `{DATA_VERSION}`. Seasons requested: "
        f"{', '.join(result.seasons) or 'none available'}.",
        f"Model version: `{MODEL_VERSION}`. Feature version: `{FEATURE_VERSION}`. "
        f"Policy: `{DEFAULT_POLICY.value}`.",
        "Only `PlayerGameweekPerformance` rows passing both `available_at <= cutoff` and "
        "`ingested_at <= cutoff` were used as features. No FPL snapshots, season totals, "
        "future ownership, prices, xP, transfers, availability, lineups, or post-match "
        "data were substituted.",
        f"Total evaluated candidate rows: N = {len(result.rows)}. "
        f"Baseline rows: N = {len(result.rows)} for each required baseline. "
        f"Fold prediction total: N = "
        f"{sum(int(fold.get('n_predictions', 0)) for fold in result.folds)}. "
        f"Excluded rows: {sum(result.exclusions.values())} ({result.exclusions})."
        + (f" Data access error: {result.data_error}." if result.data_error else ""),
        "",
        "## Temporal policy",
        "Each fold trains on all prior chronological folds and predicts the next gameweek. "
        "The cutoff is one hour before the canonical gameweek deadline. No random split is used.",
        f"Evaluation folds: {len(result.folds)}. First folds reserved for initial training: 3.",
        "",
        "## Metrics",
        f"All metric rows use the evaluated candidate denominator N = {len(result.rows)}. "
        "Baselines are scored on the same evaluated rows; no baseline-only rows are included.",
        _markdown_metrics(overall),
        "",
        "## Expected-Minutes Ensemble",
        "The blend uses `E_blend = w * E_model + (1 - w) * E_recent`. For every outer fold, "
        "the weight is selected from a chronological inner training prefix and its next "
        "inner validation period, then frozen before the outer unseen evaluation fold. "
        "Outer actual minutes never participate in weight selection. The candidate "
        "probability outputs are copied unchanged.",
        "Selected weights and inner/outer windows are recorded in each fold's provenance.",
        "",
        "## Calibration",
    ]
    for target in ("start", "appearance", "60_plus"):
        table = reliability_table(result.rows, "candidate", target)
        lines.extend([f"### {target}", _markdown_reliability(table)])
    lines.extend(
        [
            "",
            "## Season breakdown",
            _markdown_breakdown(build_breakdown(result.rows, models, "season")),
            "",
            "## Position breakdown",
            _markdown_breakdown(build_breakdown(result.rows, models, "position")),
            "",
            "## Minutes-tier breakdown",
            _markdown_breakdown(build_breakdown(result.rows, models, "minutes_tier")),
            "",
            "## Failure modes",
            "No empirical failure mode can be established without canonical historical rows."
            if not result.rows
            else "Inspect the highest-error breakdowns above; small groups must not be "
            "treated as stable evidence.",
            "",
            "## Statistical limitations",
            "Metrics are descriptive and have no confidence intervals. Fold-level dependence "
            "and player-level repeated observations limit independence. Empty or small groups "
            "are reported with N and are not used for promotion claims.",
            "",
            "## Promotion decision",
            decision,
            "",
            "## Reproduction",
            "Run `python scripts/evaluate_minutes_walkforward.py --report "
            "docs/STAGE_2A_MINUTES_VALIDATION.md` against the configured canonical database.",
            "",
        ]
    )
    return "\n".join(lines)


def _markdown_metrics(metrics: dict[str, dict[str, float | int | None]]) -> str:
    lines = [
        "| Model | N | MAE | RMSE | Start Brier | Start Log loss | Start ECE | "
        "Appearance Brier | Appearance Log loss | 60+ Brier | 60+ Log loss |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, values in metrics.items():
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                name,
                values["n"],
                *[
                    _fmt(values[key])
                    for key in (
                        "mae",
                        "rmse",
                        "start_brier",
                        "start_log_loss",
                        "start_calibration_error",
                        "appearance_brier",
                        "appearance_log_loss",
                        "sixty_plus_brier",
                        "sixty_plus_log_loss",
                    )
                ],
            )
        )
    return "\n".join(lines)


def _markdown_reliability(table: list[dict[str, float | int | None]]) -> str:
    lines = ["| Probability bucket | Predicted | Observed | N |", "|---|---:|---:|---:|"]
    for row in table:
        lines.append(
            f"| {row['bucket']} | {_fmt(row['predicted_probability'])} | "
            f"{_fmt(row['observed_frequency'])} | {row['n']} |"
        )
    return "\n".join(lines)


def _markdown_breakdown(breakdown: dict[str, dict[str, dict[str, float | int | None]]]) -> str:
    if not breakdown:
        return "No canonical rows available."
    lines = [
        "| Group | Model | N | MAE | Start Brier | Appearance Brier | 60+ Brier |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for group, model_values in breakdown.items():
        for model, values in model_values.items():
            lines.append(
                f"| {group} | {model} | {values['n']} | {_fmt(values['mae'])} | "
                f"{_fmt(values['start_brier'])} | {_fmt(values['appearance_brier'])} | "
                f"{_fmt(values['sixty_plus_brier'])} |"
            )
    return "\n".join(lines)


def _position_name(code: int | None) -> str:
    return {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}.get(code, "UNKNOWN")


def _minutes_tier(minutes: float) -> str:
    if minutes <= 0:
        return "0"
    if minutes < 30:
        return "1-29"
    if minutes < 60:
        return "30-59"
    if minutes < 90:
        return "60-89"
    return "90+"


def _frequency_group(features: dict[str, float]) -> str:
    starts = features.get("starts_last_10", 0)
    minutes = features.get("minutes_last_10", 0)
    if starts >= 8 and minutes >= 600:
        return "nailed"
    if starts >= 5:
        return "regular_starter"
    if starts >= 2:
        return "rotation"
    if minutes > 0:
        return "low_minute"
    return "bench"


def _log_loss(probabilities: np.ndarray, actual: np.ndarray) -> float:
    clipped = np.clip(probabilities, 1e-12, 1 - 1e-12)
    return float(-np.mean(actual * np.log(clipped) + (1 - actual) * np.log(1 - clipped)))


def calibration_error(probabilities: np.ndarray, actual: np.ndarray) -> float | None:
    if not len(probabilities):
        return None
    error = 0.0
    for bucket in range(10):
        mask = (probabilities >= bucket / 10) & (
            probabilities <= (bucket + 1) / 10 if bucket == 9 else probabilities < (bucket + 1) / 10
        )
        if mask.any():
            error += float(mask.mean()) * abs(
                float(probabilities[mask].mean()) - float(actual[mask].mean())
            )
    return error


def _roc_auc(probabilities: np.ndarray, actual: np.ndarray) -> float | None:
    positives = probabilities[actual == 1]
    negatives = probabilities[actual == 0]
    if not len(positives) or not len(negatives):
        return None
    return float(
        (
            np.greater.outer(positives, negatives).sum()
            + 0.5 * np.equal.outer(positives, negatives).sum()
        )
        / (len(positives) * len(negatives))
    )


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _mean(values: list[float]) -> float | None:
    return float(np.mean(values)) if values else None


def _fmt(value: Any) -> str:
    return (
        "NA"
        if value is None or (isinstance(value, float) and math.isnan(value))
        else f"{float(value):.4f}"
    )
