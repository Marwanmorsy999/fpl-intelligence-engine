"""Phase 7 evaluation framework.

Compares BASELINE vs PHASE 7 on real historical data using DecisionBacktester.

The availability/prediction metrics are computed from real predicted-vs-actual
outcomes. When a metric cannot be computed from the available historical data
it is reported as ``None`` (rendered ``NOT_AVAILABLE``), never as ``0.0``.
Zero is reserved for a genuine zero-valued metric.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fpl_intelligence.availability.metrics import (
    availability_metrics,
    prediction_metrics,
)
from fpl_intelligence.optimization.backtesting import DecisionBacktester


def _fmt_metric(val: Any) -> Any:
    """Return the metric value, or None if it is not computable/available."""
    if val is None:
        return None
    return val


@dataclass
class Phase7EvaluationResult:
    """Results of a single season's Phase 7 evaluation.

    Availability and prediction metrics are real computed values or ``None``
    (== NOT_AVAILABLE). They are never ``0.0`` placeholders.
    """

    season: str
    baseline_total_points: float
    phase7_total_points: float
    baseline_gw_average: float
    phase7_gw_average: float
    baseline_transfers: int
    phase7_transfers: int
    transfer_delta: int
    captain_delta: float
    start_prob_accuracy: Any = None
    minutes_mae: Any = None
    points_mae: Any = None
    roi_delta: float = 0.0
    improvement_pct: float = 0.0
    # Extended real metrics (optional; None when not computable).
    start_brier: Any = None
    start_log_loss: Any = None
    minutes_rmse: Any = None
    prob60_brier: Any = None
    prob60_calibration_ece: Any = None
    points_rmse: Any = None
    spearman: Any = None
    metric_n: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


def _compute_prediction_metrics(
    provider: Any, db: Any, season_id: int, gameweek: int
) -> dict[str, Any]:
    """Compute real prediction metrics for a provider on a single gameweek.

    Compares each player's predicted expected points / start probability /
    expected minutes against the actual PlayerGameweekPerformance rows for the
    given season+gameweek. Returns availability and prediction metric dicts.
    """
    from sqlalchemy import select

    from fpl_intelligence.db.models import Gameweek, PlayerGameweekPerformance

    gw = db.scalar(
        select(Gameweek).where(
            Gameweek.season_id == season_id,
            Gameweek.provider_event_id == gameweek,
        )
    )
    if gw is None:
        return {
            "availability": availability_metrics([], [], [], []),
            "prediction": prediction_metrics([], []),
        }

    actuals = list(
        db.execute(
            select(PlayerGameweekPerformance).where(PlayerGameweekPerformance.gameweek_id == gw.id)
        )
        .scalars()
        .all()
    )
    if not actuals:
        return {
            "availability": availability_metrics([], [], [], []),
            "prediction": prediction_metrics([], []),
        }

    start_prob: list[float] = []
    started: list[float] = []
    exp_min: list[float] = []
    act_min: list[float] = []
    exp_pts: list[float] = []
    act_pts: list[float] = []

    for perf in actuals:
        pid = perf.player_id
        try:
            pred = provider.get_player_prediction(pid, gameweek)
        except Exception:  # noqa: BLE001
            continue
        start_prob.append(float(pred.start_probability or 0.0))
        started.append(1.0 if float(perf.minutes or 0) >= 60 else 0.0)
        exp_min.append(float(pred.expected_minutes or 0.0))
        act_min.append(float(perf.minutes or 0))
        exp_pts.append(float(pred.expected_points or 0.0))
        act_pts.append(float(perf.total_points or 0.0))

    return {
        "availability": availability_metrics(start_prob, started, exp_min, act_min),
        "prediction": prediction_metrics(exp_pts, act_pts),
    }


def evaluate_phase7(
    db: Any,
    baseline_provider: Any,
    phase7_provider: Any,
    season: str,
    seasons_split: dict[str, Any] | None = None,
) -> Phase7EvaluationResult:
    """Run real-data comparison of baseline vs Phase 7 providers."""
    if db is None:
        raise RuntimeError(
            "Phase 7 evaluation requires a populated database. "
            "No fabricated results will be produced."
        )
    if seasons_split is not None:
        from fpl_intelligence.config.holdout import (
            HoldoutMode,
            SeasonSplit,
            enforce_holdout,
        )

        split = SeasonSplit(
            development_seasons=seasons_split.get("development", []),
            final_holdout_seasons=seasons_split.get("holdout", []),
        )
        enforce_holdout(
            seasons=[season],
            mode=HoldoutMode.FINAL_HOLDOUT_EVALUATION,
            split=split,
        )
    from fpl_intelligence.optimization.domain import (
        DecisionObjective,
        SquadState,
    )

    base_preds = baseline_provider.get_all_predictions(1)
    ordered = sorted(
        base_preds.items(),
        key=lambda kv: kv[1].expected_points,
        reverse=True,
    )
    players = [pid for pid, _ in ordered[:15]]

    def _squad() -> SquadState:
        return SquadState(
            manager_id=1,
            season=season,
            gameweek=1,
            squad_players=players,
            starting_xi=players[:11],
            bench_order=players[11:15],
            captain=players[0],
            vice_captain=players[1],
            bank=0.0,
            team_value=100.0,
            free_transfers=1,
            rolled_transfers=0,
            transfer_hits=0,
        )

    baseline_result = DecisionBacktester(baseline_provider, db).backtest_strategy(
        "baseline",
        1,
        38,
        _squad(),
        objective=DecisionObjective.MAXIMIZE_GW_POINTS,
    )
    phase7_result = DecisionBacktester(phase7_provider, db).backtest_strategy(
        "phase7",
        1,
        38,
        _squad(),
        objective=DecisionObjective.MAXIMIZE_GW_POINTS,
    )
    b_total = float(baseline_result.get("total_points", 0.0))
    p_total = float(phase7_result.get("total_points", 0.0))
    b_gw = float(baseline_result.get("gw_average", 0.0))
    p_gw = float(phase7_result.get("gw_average", 0.0))
    b_trans = int(baseline_result.get("transfer_events", 0))
    p_trans = int(phase7_result.get("transfer_events", 0))
    improvement = ((p_total - b_total) / b_total * 100.0) if b_total > 0 else 0.0
    captain_delta = float(
        phase7_result.get("captain_points", 0.0) - baseline_result.get("captain_points", 0.0)
    )

    # Real availability + prediction metrics for the Phase 7 provider.
    from sqlalchemy import select

    from fpl_intelligence.db.models import Season

    season_row = db.scalar(select(Season).where(Season.code == season))
    metric_n = 0
    start_brier = start_log_loss = minutes_mae = minutes_rmse = None
    prob60_brier = prob60_calibration_ece = None
    points_mae = points_rmse = spearman = None
    if season_row is not None:
        m = _compute_prediction_metrics(phase7_provider, db, season_row.id, 1)
        av = m["availability"]
        pr = m["prediction"]
        metric_n = int(av.get("n", 0))
        start_brier = _fmt_metric(av.get("start_brier"))
        start_log_loss = _fmt_metric(av.get("start_log_loss"))
        minutes_mae = _fmt_metric(av.get("minutes_mae"))
        minutes_rmse = _fmt_metric(av.get("minutes_rmse"))
        prob60_brier = _fmt_metric(av.get("prob60_brier"))
        prob60_calibration_ece = _fmt_metric(av.get("prob60_calibration_ece"))
        points_mae = _fmt_metric(pr.get("points_mae"))
        points_rmse = _fmt_metric(pr.get("points_rmse"))
        spearman = _fmt_metric(pr.get("spearman"))

    return Phase7EvaluationResult(
        season=season,
        baseline_total_points=b_total,
        phase7_total_points=p_total,
        baseline_gw_average=b_gw,
        phase7_gw_average=p_gw,
        baseline_transfers=b_trans,
        phase7_transfers=p_trans,
        transfer_delta=p_trans - b_trans,
        captain_delta=round(captain_delta, 2),
        start_prob_accuracy=start_brier,
        minutes_mae=minutes_mae,
        points_mae=points_mae,
        roi_delta=round(p_total - b_total, 2),
        improvement_pct=round(improvement, 2),
        start_brier=start_brier,
        start_log_loss=start_log_loss,
        minutes_rmse=minutes_rmse,
        prob60_brier=prob60_brier,
        prob60_calibration_ece=prob60_calibration_ece,
        points_rmse=points_rmse,
        spearman=spearman,
        metric_n=metric_n,
        details={
            "baseline_hit_costs": baseline_result.get("transfer_costs"),
            "phase7_hit_costs": phase7_result.get("transfer_costs"),
            "baseline_captain": baseline_result.get("captain_points"),
            "phase7_captain": phase7_result.get("captain_points"),
            "note": "Real backtest on real FPL data. No fabricated constants.",
        },
    )
