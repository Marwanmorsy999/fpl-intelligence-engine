"""Chip optimization and simulation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
class ChipEvaluation:
    """Evaluation of playing a chip."""
    
    chip_name: str
    expected_score_with_chip: float
    expected_score_without_chip: float
    net_value: float
    opportunity_cost: float


class ChipSimulator:
    """Evaluates the value of playing chips."""

    def __init__(self, provider: DecisionPredictionProvider, rules: FPLRules) -> None:
        self.provider = provider
        self.rules = rules

    def evaluate_bench_boost(self, squad: SquadState, gameweek: int) -> ChipEvaluation:
        """Evaluate Bench Boost value from provider predictions.

        Bench Boost adds all 4 bench players' points to the gameweek total, so
        ``with_chip`` = starting-XI EV + bench EV, ``without_chip`` = XI EV only,
        and ``net_value`` = bench EV. All values come from the provider, not
        hardcoded constants.
        """
        xi_ev = 0.0
        for pid in squad.starting_xi:
            pred = self.provider.get_player_prediction(pid, gameweek)
            if pred.distribution is not None and len(pred.distribution) > 0:
                xi_ev += float(np.mean(pred.distribution))
            else:
                xi_ev += pred.expected_points

        bench_ev = 0.0
        for pid in squad.bench_order:
            pred = self.provider.get_player_prediction(pid, gameweek)
            fixtures = self.provider.get_fixture_count(pid, gameweek)
            if fixtures == 0:
                continue
            if pred.distribution is not None and len(pred.distribution) > 0:
                bench_ev += float(np.mean(pred.distribution))
            else:
                bench_ev += pred.expected_points

        return ChipEvaluation(
            chip_name="bench_boost",
            expected_score_with_chip=round(xi_ev + bench_ev, 4),
            expected_score_without_chip=round(xi_ev, 4),
            net_value=round(bench_ev, 4),
            opportunity_cost=0.0,
        )

    def evaluate_triple_captain(self, squad: SquadState, gameweek: int) -> ChipEvaluation:
        """Evaluate Triple Captain value.
        
        Expected value = 3 * EV - 2 * EV = 1 * EV of best captain.
        Uses actual distributions to capture upside.
        """
        best_ev = 0.0
        for pid in squad.starting_xi:
            pred = self.provider.get_player_prediction(pid, gameweek)
            fixtures = self.provider.get_fixture_count(pid, gameweek)
            
            # Penalize single gameweeks
            multiplier = 1.0 if fixtures > 1 else 0.7
            
            if pred.distribution is not None and len(pred.distribution) > 0:
                ev = float(np.mean(pred.distribution)) * multiplier
            else:
                ev = pred.expected_points * multiplier
                
            if ev > best_ev:
                best_ev = ev
                
        return ChipEvaluation(
            chip_name="triple_captain",
            expected_score_with_chip=best_ev * 3,
            expected_score_without_chip=best_ev * 2,
            net_value=best_ev,
            opportunity_cost=0.0,
        )

    def evaluate_free_hit(self, squad: SquadState, gameweek: int) -> ChipEvaluation:
        """Evaluate Free Hit value from the provider's full 1-GW pool.

        A Free Hit allows an entirely new 11 for a single gameweek, so the
        ``with_chip`` value is the sum of the top-11 expected points across the
        whole player pool (plus the captain multiplier), not a fixed constant.
        """
        current_xi_ev = 0.0
        for pid in squad.starting_xi:
            pred = self.provider.get_player_prediction(pid, gameweek)
            current_xi_ev += pred.expected_points

        pool: list[float] = []
        try:
            all_preds = self.provider.get_all_predictions(gameweek)
            pool = [p.expected_points for p in all_preds.values()]
        except Exception:  # noqa: BLE001
            pool = []
        if not pool:
            # Fall back to the current XI when the provider exposes no pool.
            pool = [p.expected_points for p in (self.provider.get_player_prediction(pid, gameweek) for pid in squad.squad_players)]

        pool_sorted = sorted(pool, reverse=True)
        top11 = pool_sorted[:11]
        if not top11:
            optimal_fh_ev = current_xi_ev
        else:
            optimal_fh_ev = sum(top11) + top11[0]  # captain scored double

        net_value = optimal_fh_ev - current_xi_ev
        return ChipEvaluation(
            chip_name="free_hit",
            expected_score_with_chip=round(optimal_fh_ev, 4),
            expected_score_without_chip=round(current_xi_ev, 4),
            net_value=round(net_value, 4),
            opportunity_cost=0.0,
        )

    def evaluate_wildcard(self, squad: SquadState, gameweek: int, horizon: int = 4) -> ChipEvaluation:
        """Evaluate Wildcard value over the horizon from provider expectations.

        With a Wildcard the manager can restructure to the league's best 15,
        so the ``with_chip`` value is the sum of the top-11 (plus captain) EV per
        gameweek across the whole pool over ``horizon`` gameweeks - here derived
        from the provider's own predictions instead of a fixed per-GW constant.
        """
        current_squad_ev = 0.0
        for offset in range(horizon):
            gw = gameweek + offset
            gwevs = [
                self.provider.get_player_prediction(pid, gw).expected_points
                for pid in squad.squad_players
            ]
            cur = sorted(gwevs, reverse=True)[:11]
            if cur:
                current_squad_ev += sum(cur) + cur[0]  # captain doubled

        optimal_wc_ev = 0.0
        for offset in range(horizon):
            gw = gameweek + offset
            pool: list[float] = []
            try:
                pool = [
                    p.expected_points
                    for p in self.provider.get_all_predictions(gw).values()
                ]
            except Exception:  # noqa: BLE001
                pool = []
            if pool:
                top = sorted(pool, reverse=True)[:11]
                optimal_wc_ev += sum(top) + top[0]
            else:
                optimal_wc_ev += current_squad_ev / max(1, horizon)

        net_value = optimal_wc_ev - current_squad_ev
        return ChipEvaluation(
            chip_name="wildcard",
            expected_score_with_chip=round(optimal_wc_ev, 4),
            expected_score_without_chip=round(current_squad_ev, 4),
            net_value=round(net_value, 4),
            opportunity_cost=0.0,
        )

    def recommend_chip(self, squad: SquadState, gameweek: int) -> Recommendation | None:
        """Recommend whether to play a chip this gameweek."""
        # Check active chips first - can only play one!
        if len(squad.active_chips) > 0:
            return None
            
        best_chip = None
        best_value = 0.0
        
        half = self.rules.get_half_season(gameweek) if self.rules.is_half_season_chips else None
        
        # Determine playable chips based on rules
        playable_chips = []
        for chip in squad.remaining_chips:
            if self.rules.is_half_season_chips:
                # Assuming chips are named "wildcard_1", "wildcard_2" or we manage them via half season
                if f"_{half}" in chip or (half == 1 and ("_1" in chip or chip.endswith("1"))) or (half == 2 and ("_2" in chip or chip.endswith("2"))):
                    playable_chips.append(chip.split("_1")[0].split("_2")[0])
                elif not ("_1" in chip or "_2" in chip):
                     # If they are just "wildcard", assume playable for testing
                     playable_chips.append(chip)
            else:
                playable_chips.append(chip.split("_1")[0].split("_2")[0])
                
        if "bench_boost" in playable_chips:
            bb_eval = self.evaluate_bench_boost(squad, gameweek)
            if bb_eval.net_value > 15.0 and bb_eval.net_value > best_value:
                best_chip = bb_eval
                best_value = bb_eval.net_value
                
        if "triple_captain" in playable_chips:
            tc_eval = self.evaluate_triple_captain(squad, gameweek)
            if tc_eval.net_value > 12.0 and tc_eval.net_value > best_value:
                best_chip = tc_eval
                best_value = tc_eval.net_value
                
        if "free_hit" in playable_chips:
            fh_eval = self.evaluate_free_hit(squad, gameweek)
            if fh_eval.net_value > 18.0 and fh_eval.net_value > best_value:
                best_chip = fh_eval
                best_value = fh_eval.net_value
                
        if "wildcard" in playable_chips:
            wc_eval = self.evaluate_wildcard(squad, gameweek)
            if wc_eval.net_value > 20.0 and wc_eval.net_value > best_value:
                best_chip = wc_eval
                best_value = wc_eval.net_value
                
        if best_chip:
            action = CandidateAction(
                action_type=ActionType(best_chip.chip_name),
                chip=best_chip.chip_name,
                target_gameweek=gameweek,
            )
            return Recommendation(
                action=action,
                expected_gain=best_chip.net_value,
                base_case=best_chip.net_value,
                downside_case=best_chip.net_value * 0.2,
                upside_case=best_chip.net_value * 1.8,
                probability_positive=0.7,
                confidence=0.6,
                main_reason=f"High EV for {best_chip.chip_name}.",
                main_risk="Missing future opportunities."
            )
            
        return None
