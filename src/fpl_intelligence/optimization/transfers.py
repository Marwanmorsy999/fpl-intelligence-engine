"""Transfer optimization and planning."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from fpl_intelligence.optimization.domain import (
    ActionType,
    CandidateAction,
    Recommendation,
    SquadState,
)
from fpl_intelligence.optimization.provider import DecisionPredictionProvider
from fpl_intelligence.optimization.rules import FPLRules


@dataclass
class TransferEvaluation:
    """Evaluation of a specific transfer option."""

    transfers_in: list[int]
    transfers_out: list[int]
    hit_cost: int
    expected_points_gain: float  # over the horizon
    net_points: float  # expected_points_gain - hit_cost
    probability_beat_roll: float
    is_valid: bool
    reason: str = ""


class TransferOptimizer:
    """Evaluates and compares transfer options."""

    def __init__(self, provider: DecisionPredictionProvider, rules: FPLRules) -> None:
        self.provider = provider
        self.rules = rules

    def evaluate_transfer(
        self,
        squad: SquadState,
        player_out: int,
        player_in: int,
        horizon: int = 4,
    ) -> TransferEvaluation:
        """Evaluate a single 1-to-1 transfer over a planning horizon."""

        expected_gain = 0.0
        var_in = 0.0
        var_out = 0.0

        # Calculate EV and variance over horizon
        for offset in range(horizon):
            gw = squad.gameweek + offset
            pred_out = self.provider.get_player_prediction(player_out, gw)
            pred_in = self.provider.get_player_prediction(player_in, gw)

            # Using actual distributions to get EV and Variance
            if pred_in.distribution is not None and len(pred_in.distribution) > 0:
                ev_in = float(np.mean(pred_in.distribution))
                v_in = float(np.var(pred_in.distribution))
            else:
                ev_in = pred_in.expected_points
                v_in = ev_in * 1.5  # Heuristic variance

            if pred_out.distribution is not None and len(pred_out.distribution) > 0:
                ev_out = float(np.mean(pred_out.distribution))
                v_out = float(np.var(pred_out.distribution))
            else:
                ev_out = pred_out.expected_points
                v_out = ev_out * 1.5

            expected_gain += ev_in - ev_out
            var_in += v_in
            var_out += v_out

        hit_cost = 0
        if squad.free_transfers < 1:
            hit_cost = self.rules.transfer_hit_cost

        net_points = expected_gain - hit_cost

        total_var = var_in + var_out
        prob_beat = 0.5
        if total_var > 0:
            import scipy.stats  # type: ignore

            # P(Gain > HitCost)
            prob_beat = float(
                scipy.stats.norm.sf(hit_cost, loc=expected_gain, scale=np.sqrt(total_var))
            )
        else:
            prob_beat = max(0.0, min(1.0, 0.5 + (net_points * 0.05)))

        return TransferEvaluation(
            transfers_in=[player_in],
            transfers_out=[player_out],
            hit_cost=hit_cost,
            expected_points_gain=expected_gain,
            net_points=net_points,
            probability_beat_roll=prob_beat,
            is_valid=True,
        )


class MultiTransferPlanner:
    """Plans multiple transfers using heuristic pruning and beam search."""

    def __init__(
        self, optimizer: TransferOptimizer, provider: DecisionPredictionProvider, rules: FPLRules
    ) -> None:
        self.optimizer = optimizer
        self.provider = provider
        self.rules = rules

    def _horizon_expected_points(
        self,
        player_ids: list[int],
        start_gameweek: int,
        horizon: int,
    ) -> dict[int, float]:
        """Return lightweight expected-point sums for a player horizon.

        The bulk provider deliberately omits predictive distributions, because
        transfer candidate pruning needs only expected points. Using it here
        avoids constructing thousands of NumPy samples just to rank targets.
        Missing players fall back to the normal single-player provider path so
        this optimization does not change coverage semantics.
        """
        totals = {int(pid): 0.0 for pid in player_ids}
        wanted = set(totals)
        for offset in range(horizon):
            gw = start_gameweek + offset
            try:
                pool = self.provider.get_all_predictions(gw)
            except Exception:
                pool = {}
            missing: list[int] = []
            for pid in player_ids:
                pred = pool.get(int(pid))
                if pred is None:
                    missing.append(int(pid))
                else:
                    totals[int(pid)] += float(pred.expected_points)
            if missing:
                for pid in missing:
                    try:
                        pred = self.provider.get_player_prediction(pid, gw)
                    except Exception:
                        continue
                    if int(pid) in wanted:
                        totals[int(pid)] += float(pred.expected_points)
        return totals

    def generate_candidates(
        self,
        squad: SquadState,
        player_positions: dict[int, int],
        player_prices: dict[int, float],
        player_teams: dict[int, int],
        horizon: int = 4,
    ) -> Recommendation:
        """Generate the best transfer recommendation for the squad.

        Compares:
        - Roll transfer (0 transfers)
        - 1 Free Transfer (if available)
        - Hits (if net EV is positive)
        """
        all_players_pool = list(player_positions.keys())

        # 1. Option A: Roll transfer
        roll_action = CandidateAction(action_type=ActionType.ROLL, horizon=horizon)
        best_eval = TransferEvaluation([], [], 0, 0.0, 0.0, 0.5, True, "Roll transfer.")
        best_action = roll_action

        # Bulk lightweight path: expected-point pruning does not require
        # predictive distributions. Reuses the request-local full pools so the
        # same GW is never regenerated across optimizer stages.
        horizon_pools = self._horizon_expected_points(
            all_players_pool,
            squad.gameweek,
            horizon,
        )

        squad_evs = {pid: horizon_pools.get(pid, 0.0) for pid in squad.squad_players}
        weakest_links = sorted(squad.squad_players, key=lambda p: squad_evs[p])[:3]

        target_evs = {
            pid: ev
            for pid, ev in horizon_pools.items()
            if pid not in squad.squad_players
        }

        top_targets = []
        for pos in [1, 2, 3, 4]:
            pos_targets = [p for p in target_evs if player_positions[p] == pos]
            pos_targets = sorted(pos_targets, key=lambda p: target_evs[p], reverse=True)[:10]
            top_targets.extend(pos_targets)

        # 3. Evaluate 1-transfer combinations
        for p_out in weakest_links:
            pos_out = player_positions[p_out]
            price_out = player_prices.get(p_out, 0.0)

            for p_in in top_targets:
                if player_positions[p_in] != pos_out:
                    continue  # Must be same position for simple 1-to-1

                price_in = player_prices.get(p_in, 0.0)
                if squad.bank + price_out < price_in:
                    continue  # Budget constraint

                team_in = player_teams.get(p_in)
                current_from_team = sum(
                    1 for p in squad.squad_players if player_teams.get(p) == team_in
                )
                if current_from_team >= self.rules.max_players_per_club:
                    continue

                eval_obj = self.optimizer.evaluate_transfer(squad, p_out, p_in, horizon)

                flexibility_penalty = (
                    0.5
                    if squad.free_transfers > 0
                    and squad.rolled_transfers < self.rules.max_rolled_transfers
                    else 0.0
                )

                if eval_obj.net_points - flexibility_penalty > best_eval.net_points:
                    best_eval = eval_obj
                    best_action = CandidateAction(
                        action_type=ActionType.TRANSFER,
                        transfers_in=[p_in],
                        transfers_out=[p_out],
                        hit_cost=eval_obj.hit_cost,
                        horizon=horizon,
                    )

        action_type_str = (
            "Hit"
            if best_eval.hit_cost > 0
            else (
                "Free Transfer"
                if best_action.action_type == ActionType.TRANSFER
                else "Roll Transfer"
            )
        )

        # Deciding margin: EV gain over the next-best option (roll).
        margin = round(best_eval.net_points, 2)
        reason = f"{action_type_str}: +{margin} EV over next option."

        return Recommendation(
            action=best_action,
            expected_gain=best_eval.net_points,
            base_case=best_eval.net_points,
            downside_case=best_eval.net_points - 3.0,
            upside_case=best_eval.net_points + 5.0,
            probability_positive=best_eval.probability_beat_roll,
            confidence=0.7,
            main_reason=reason,
            main_risk="Opportunity cost of transfer flexibility."
            if best_action.action_type == ActionType.TRANSFER
            else "Missed points on bench.",
        )
