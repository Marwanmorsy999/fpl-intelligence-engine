"""Chip optimization and simulation."""

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

_FORMATION_MIN = {1: 1, 2: 3, 3: 2, 4: 1}
_FORMATION_MAX = {1: 1, 2: 5, 3: 5, 4: 3}
_POSITION_SLOTS = {1: 2, 2: 5, 3: 5, 4: 3}  # squad slots per position


def _formation_valid_top11(
    pool_by_pos: dict[int, list[float]],
) -> float:
    """Return the best achievable 11-player EV that satisfies FPL formation rules.

    For each position, greedily take as many high-EV players as allowed by the
    formation max, subject to always leaving room for the formation minimums of
    the remaining positions, then pick the captain (highest EV) to double.

    Args:
        pool_by_pos: Dict mapping position_code -> sorted-descending list of EV values.

    Returns:
        Sum of the optimal 11 EVs plus the captain's EV (captain doubled).
    """
    # Enforce squad-slot upper bound and formation upper bound
    chosen: list[float] = []
    remaining_slots = 11
    positions = [1, 2, 3, 4]

    # First pass: satisfy minimums
    for pos in positions:
        n = min(_FORMATION_MIN[pos], len(pool_by_pos.get(pos, [])))
        chosen.extend(pool_by_pos.get(pos, [])[:n])
        remaining_slots -= n

    # Second pass: greedily fill remaining slots up to formation max
    # Build remaining available players per position
    avail: dict[int, list[float]] = {}
    for pos in positions:
        already_taken = _FORMATION_MIN[pos]
        avail[pos] = pool_by_pos.get(pos, [])[already_taken:_FORMATION_MAX[pos]]

    # Merge all available into one pool and take best
    all_avail = sorted(
        [(ev, pos) for pos, evs in avail.items() for ev in evs],
        reverse=True,
    )
    for ev, _pos in all_avail[:remaining_slots]:
        chosen.append(ev)

    # Double the best player (captain)
    total = sum(chosen)
    captain_ev = max(chosen) if chosen else 0.0
    return total + captain_ev  # captain's points count twice


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

        # Build per-position pools and use formation-valid selection
        # so the EV estimate can't include 11 GKs or other illegal formations.
        pool_by_pos: dict[int, list[float]] = {1: [], 2: [], 3: [], 4: []}
        try:
            all_preds_map = self.provider.get_all_predictions(gameweek)
            for p in all_preds_map.values():
                pos = getattr(p, 'position_code', None) or 3
                pool_by_pos.setdefault(pos, []).append(p.expected_points)
        except Exception:  # noqa: BLE001
            pass
        for pos in pool_by_pos:
            pool_by_pos[pos].sort(reverse=True)
        optimal_fh_ev = (
            current_xi_ev
            if not any(pool_by_pos.values())
            else _formation_valid_top11(pool_by_pos)
        )

        net_value = optimal_fh_ev - current_xi_ev
        return ChipEvaluation(
            chip_name="free_hit",
            expected_score_with_chip=round(optimal_fh_ev, 4),
            expected_score_without_chip=round(current_xi_ev, 4),
            net_value=round(net_value, 4),
            opportunity_cost=0.0,
        )

    def evaluate_wildcard(
        self, squad: SquadState, gameweek: int, horizon: int = 4
    ) -> ChipEvaluation:
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
                pool = [p.expected_points for p in self.provider.get_all_predictions(gw).values()]
            except Exception:  # noqa: BLE001
                pool = []
            if pool:
                # Use formation-valid selection to avoid illegal team compositions
                wc_pool_by_pos: dict[int, list[float]] = {1: [], 2: [], 3: [], 4: []}
                try:
                    gw_preds = self.provider.get_all_predictions(gw)
                    for p in gw_preds.values():
                        pos = getattr(p, 'position_code', None) or 3
                        wc_pool_by_pos.setdefault(pos, []).append(p.expected_points)
                except Exception:  # noqa: BLE001
                    for ev in pool:
                        wc_pool_by_pos[3].append(ev)
                for pos in wc_pool_by_pos:
                    wc_pool_by_pos[pos].sort(reverse=True)
                optimal_wc_ev += _formation_valid_top11(wc_pool_by_pos)
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
                # Chips are named "wildcard_1"/"wildcard_2" or managed via half season
                if (
                    f"_{half}" in chip
                    or (half == 1 and ("_1" in chip or chip.endswith("1")))
                    or (half == 2 and ("_2" in chip or chip.endswith("2")))
                ):
                    playable_chips.append(chip.split("_1")[0].split("_2")[0])
                elif not ("_1" in chip or "_2" in chip):
                    # If they are just "wildcard", assume playable for testing
                    playable_chips.append(chip)
            else:
                playable_chips.append(chip.split("_1")[0].split("_2")[0])

        # Thresholds are now fractions of the expected XI EV so they scale
        # with the game state rather than using season-agnostic magic numbers.
        xi_ev_baseline = sum(
            self.provider.get_player_prediction(pid, gameweek).expected_points
            for pid in squad.starting_xi
        ) if squad.starting_xi else 50.0
        # BB worthwhile if bench adds >=40% of XI EV; TC if captain adds >=26%;
        # FH if gain > 45%; WC if gain > 50% of XI EV over a single GW.
        # Higher thresholds prevent triggering on uniformly weak squads.
        bb_thresh = xi_ev_baseline * 0.40
        tc_thresh = xi_ev_baseline * 0.26
        fh_thresh = xi_ev_baseline * 0.45
        wc_thresh = xi_ev_baseline * 0.50

        if "bench_boost" in playable_chips:
            bb_eval = self.evaluate_bench_boost(squad, gameweek)
            if bb_eval.net_value > bb_thresh and bb_eval.net_value > best_value:
                best_chip = bb_eval
                best_value = bb_eval.net_value

        if "triple_captain" in playable_chips:
            tc_eval = self.evaluate_triple_captain(squad, gameweek)
            if tc_eval.net_value > tc_thresh and tc_eval.net_value > best_value:
                best_chip = tc_eval
                best_value = tc_eval.net_value

        if "free_hit" in playable_chips:
            fh_eval = self.evaluate_free_hit(squad, gameweek)
            if fh_eval.net_value > fh_thresh and fh_eval.net_value > best_value:
                best_chip = fh_eval
                best_value = fh_eval.net_value

        if "wildcard" in playable_chips:
            wc_eval = self.evaluate_wildcard(squad, gameweek)
            if wc_eval.net_value > wc_thresh and wc_eval.net_value > best_value:
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
                main_risk="Missing future opportunities.",
            )

        return None
