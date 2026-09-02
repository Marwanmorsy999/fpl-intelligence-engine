"""Squad and captaincy optimization."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np

from fpl_intelligence.optimization.domain import (
    ActionType,
    CandidateAction,
    DecisionObjective,
    Recommendation,
    SquadState,
)
from fpl_intelligence.optimization.provider import DecisionPredictionProvider
from fpl_intelligence.optimization.rules import FPLRules


@dataclass
class CaptainCandidate:
    """Evaluated captain candidate."""

    player_id: int
    expected_points: float
    median: float
    p10: float
    p90: float
    variance: float
    prob_10_plus: float
    prob_15_plus: float


class CaptainOptimizer:
    """Optimizes captain selection based on predictive distributions."""

    def __init__(self, provider: DecisionPredictionProvider) -> None:
        self.provider = provider

    def evaluate_candidates(
        self, candidates: list[int], gameweek: int
    ) -> dict[int, CaptainCandidate]:
        """Evaluate a list of captain candidates."""
        results = {}
        predictions = self.provider.get_squad_predictions(candidates, [gameweek]).get(
            int(gameweek), {}
        )
        for pid in candidates:
            pred = predictions.get(int(pid))
            if pred is None:
                continue

            if pred.distribution is not None and len(pred.distribution) > 0:
                dist = pred.distribution
                median = float(np.median(dist))
                variance = float(np.var(dist))
                prob_10_plus = float(np.mean(np.array(dist) >= 10))
                prob_15_plus = float(np.mean(np.array(dist) >= 15))
                p10 = pred.floor if pred.floor > 0 else float(np.percentile(dist, 10))
                p90 = pred.ceiling if pred.ceiling > 0 else float(np.percentile(dist, 90))
                expected_points = float(np.mean(dist))
            else:
                expected_points = pred.expected_points
                median = expected_points
                variance = 0.0
                prob_10_plus = 1.0 if expected_points >= 10 else 0.0
                prob_15_plus = 1.0 if expected_points >= 15 else 0.0
                p10 = pred.floor
                p90 = pred.ceiling

            results[pid] = CaptainCandidate(
                player_id=pid,
                expected_points=expected_points,
                median=median,
                p10=p10,
                p90=p90,
                variance=variance,
                prob_10_plus=prob_10_plus,
                prob_15_plus=prob_15_plus,
            )
        return results

    def recommend_captain(
        self,
        squad: SquadState,
        objective: DecisionObjective = DecisionObjective.MAXIMIZE_GW_POINTS,
        benchmark_captain_id: int | None = None,
    ) -> Recommendation:
        """Recommend a captain based on the squad and strategic objective."""
        candidates = squad.starting_xi
        evaluated = self.evaluate_candidates(candidates, squad.gameweek)

        best_candidate: CaptainCandidate | None = None
        main_reason = ""

        if objective == DecisionObjective.PROTECT_RANK:
            if benchmark_captain_id and benchmark_captain_id in evaluated:
                best_candidate = evaluated[benchmark_captain_id]
                main_reason = "Match benchmark captain to protect rank."
            else:
                best_candidate = max(evaluated.values(), key=lambda c: (c.p10, c.expected_points))
                main_reason = "Safe captain with highest floor to minimize downside."

        elif objective == DecisionObjective.CHASE_RANK:
            best_candidate = max(evaluated.values(), key=lambda c: (c.prob_15_plus, c.p90))
            main_reason = "Differential captain with highest probability of haul."

        else:
            best_candidate = max(evaluated.values(), key=lambda c: c.expected_points)
            main_reason = "Highest expected value captain."

        if not best_candidate:
            best_candidate = evaluated[candidates[0]]

        # CandidateAction has no dedicated captain-player field. The existing
        # bridge reads `transfers_in[0]` for captain recommendations, so carry
        # the selected captain ID there. CAPTAIN actions are never interpreted
        # as actual transfers by the optimizer pipeline.
        action = CandidateAction(
            action_type=ActionType.CAPTAIN,
            transfers_in=[best_candidate.player_id],
        )
        return Recommendation(
            action=action,
            expected_gain=best_candidate.expected_points,
            base_case=best_candidate.median,
            downside_case=best_candidate.p10,
            upside_case=best_candidate.p90,
            probability_positive=best_candidate.prob_10_plus,
            confidence=0.8,
            main_reason=main_reason,
            main_risk="Rotation risk or variance"
            if best_candidate.variance > 10
            else "Low ceiling",
        )


class StartingXIOptimizer:
    """Optimizes the starting XI and bench order from a fixed 15-man squad."""

    def __init__(self, provider: DecisionPredictionProvider, rules: FPLRules) -> None:
        self.provider = provider
        self.rules = rules

    def is_valid_formation(self, positions: list[int]) -> bool:
        """Check if a list of 11 position codes forms a valid FPL formation."""
        if len(positions) != 11:
            return False

        counts = {1: 0, 2: 0, 3: 0, 4: 0}
        for pos in positions:
            counts[pos] += 1

        for pos in [1, 2, 3, 4]:
            if counts[pos] < self.rules.min_formation(pos):
                return False
            if counts[pos] > self.rules.max_formation(pos):
                return False
        return True

    def optimize_xi(
        self,
        squad_players: list[int],
        gameweek: int,
        player_positions: dict[int, int],
        objective: DecisionObjective = DecisionObjective.MAXIMIZE_GW_POINTS,
    ) -> tuple[list[int], list[int]]:
        """Return the optimal starting XI and bench order.

        Args:
            squad_players: List of 15 player IDs.
            gameweek: Current gameweek.
            player_positions: Dict mapping player_id -> position_code (1 to 4).
            objective: Optimization objective.

        Returns:
            Tuple of (starting_xi, bench_order).
        """
        predictions = {}
        batch = self.provider.get_squad_predictions(squad_players, [gameweek]).get(
            int(gameweek), {}
        )
        for pid in squad_players:
            pred = batch.get(int(pid))
            if pred is None:
                continue
            # Use actual distribution expected value (better minutes factoring)
            if pred.distribution is not None and len(pred.distribution) > 0:
                ev = float(np.mean(pred.distribution))
                ceiling = float(np.percentile(pred.distribution, 90))
                floor = float(np.percentile(pred.distribution, 10))
            else:
                ev = pred.expected_points
                ceiling = pred.ceiling
                floor = pred.floor
            predictions[pid] = {"ev": ev, "ceiling": ceiling, "floor": floor}

        if objective == DecisionObjective.CHASE_RANK:
            sort_field = "ceiling"
        elif objective == DecisionObjective.PROTECT_RANK:
            sort_field = "floor"
        else:
            sort_field = "ev"

        sorted_players = sorted(
            squad_players, key=lambda pid: predictions[pid][sort_field], reverse=True
        )

        best_xi: list[int] | None = None

        for combo in itertools.combinations(sorted_players, 11):
            positions = [player_positions[pid] for pid in combo]
            if self.is_valid_formation(positions):
                best_xi = list(combo)
                break

        if not best_xi:
            best_xi = squad_players[:11]

        bench = [pid for pid in squad_players if pid not in best_xi]

        gk_bench = [pid for pid in bench if player_positions[pid] == 1]
        outfield_bench = [pid for pid in bench if player_positions[pid] != 1]

        outfield_bench.sort(key=lambda pid: predictions[pid][sort_field], reverse=True)

        bench_order = gk_bench + outfield_bench

        return best_xi, bench_order
