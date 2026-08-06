"""Decision simulation and backtesting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from fpl_intelligence.optimization.domain import (
    CandidateAction,
    DecisionObjective,
    Recommendation,
    SquadState,
    ActionType,
)
from fpl_intelligence.optimization.provider import DecisionPredictionProvider
from fpl_intelligence.optimization.rules import FPLRules


@dataclass
class SimulatedDecisionOutcome:
    """Robust output of simulating a decision."""

    expected_score: float
    median: float
    p10: float
    p90: float
    probability_beating_alternative: float
    probability_large_downside: float


def _simulate_scores(
    squad: SquadState,
    action: CandidateAction,
    horizon: int,
    simulations: int,
    seed: int,
    provider: DecisionPredictionProvider,
) -> np.ndarray:
    """Simulate total GW scores for a candidate action over the horizon.

    Samples each player's points from their predictive distribution, applies
    the starting-XI selection (by expected value), captaincy multiplier and
    bench-boost / triple-captain multipliers, and subtracts the hit cost.

    This is a *genuine* Monte-Carlo evaluation of the candidate's distribution -
    it never falls back to a fixed normal distribution for the alternative.
    """
    np.random.seed(seed)
    total_scores = np.zeros(simulations)

    for offset in range(horizon):
        gw = squad.gameweek + offset

        # Base squad - transfers out + transfers in
        players_to_sim = list(squad.squad_players)
        if action.action_type in (ActionType.TRANSFER, ActionType.WILDCARD, ActionType.FREE_HIT):
            for p in action.transfers_out:
                if p in players_to_sim:
                    players_to_sim.remove(p)
            for p in action.transfers_in:
                players_to_sim.append(p)

        gwevs: list[tuple[int, float]] = []
        gw_dists: list[tuple[int, np.ndarray]] = []
        for pid in players_to_sim:
            pred = provider.get_player_prediction(pid, gw)
            gwevs.append((pid, pred.expected_points))

            dist = pred.distribution
            if dist is None or len(dist) == 0:
                dist = np.full(simulations, pred.expected_points)
            elif len(dist) != simulations:
                dist = np.random.choice(dist, size=simulations, replace=True)
            gw_dists.append((pid, np.asarray(dist, dtype=float)))

        # Select starting XI based on EV to avoid complex per-simulation
        # permutations while still respecting the captain multiplier below.
        gwevs.sort(key=lambda x: x[1], reverse=True)
        starting_xi_pids = {x[0] for x in gwevs[:11]}
        captain_pid = gwevs[0][0]

        for pid, dist in gw_dists:
            if pid not in starting_xi_pids:
                continue
            if action.action_type == ActionType.TRIPLE_CAPTAIN and pid == captain_pid:
                multiplier = 3.0
            elif pid == captain_pid and action.action_type != ActionType.TRIPLE_CAPTAIN:
                multiplier = 2.0
            else:
                multiplier = 1.0
            total_scores += dist * multiplier

        if action.action_type == ActionType.BENCH_BOOST:
            for pid, dist in gw_dists:
                if pid not in starting_xi_pids:
                    total_scores += dist

    total_scores -= action.hit_cost
    return total_scores


def simulate_decision(
    squad: SquadState,
    action: CandidateAction,
    horizon: int,
    simulations: int,
    seed: int,
    provider: DecisionPredictionProvider,
) -> SimulatedDecisionOutcome:
    """Simulate a specific candidate action over a horizon using predictive distributions.

    ``probability_beating_alternative`` is computed by comparing the candidate's
    simulated scores against the actual ROLL alternative (the same squad with no
    transfers), NOT against a random normal placeholder.
    """
    total_scores = _simulate_scores(squad, action, horizon, simulations, seed, provider)

    # The natural alternative is to roll the squad (do nothing). We simulate the
    # same squad state under a ROLL action for a real distributional comparison.
    roll_action = CandidateAction(action_type=ActionType.ROLL, horizon=horizon)
    roll_scores = _simulate_scores(squad, roll_action, horizon, simulations, seed + 1, provider)

    expected_score = float(np.mean(total_scores))
    median = float(np.median(total_scores))
    p10 = float(np.percentile(total_scores, 10))
    p90 = float(np.percentile(total_scores, 90))

    prob_beat = float(np.mean(total_scores > roll_scores))
    downside_threshold = max(1e-9, expected_score * 0.8)
    prob_downside = float(np.mean(total_scores < downside_threshold))

    return SimulatedDecisionOutcome(
        expected_score=expected_score,
        median=median,
        p10=p10,
        p90=p90,
        probability_beating_alternative=prob_beat,
        probability_large_downside=prob_downside,
    )



class DecisionRecorder:
    """Persists recommendations immutably."""

    def __init__(self, log_path: str = "data/decisions.jsonl") -> None:
        self.log_path = log_path

    def record_decision(
        self,
        recommendation: Recommendation,
        squad_state: SquadState,
        objective: DecisionObjective,
        provider_version: str,
    ) -> None:
        """Record an optimization decision to a log file."""
        record = {
            "timestamp": datetime.now().isoformat(),
            "manager_id": squad_state.manager_id,
            "gameweek": squad_state.gameweek,
            "season": squad_state.season,
            "objective": objective.value,
            "provider_version": provider_version,
            "squad_state": squad_state.to_dict(),
            "recommendation": recommendation.to_dict(),
        }
        
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\\n")
        except IOError:
            pass


class DecisionBacktester:
    """Tests optimization strategies historically on a real, populated database.

    Walks gameweeks, formulates transfer/starting-XI/captain decisions using the
    prediction provider, and credits *actual* historical points from the
    database. It never returns fabricated constants: if no populated database is
    available it raises, so a placeholder can never be mistaken for a result.
    """

    def __init__(
        self,
        provider: DecisionPredictionProvider,
        db: Session | None = None,
        rules: FPLRules | None = None,
    ) -> None:
        self.provider = provider
        self.db = db
        self.rules = rules or FPLRules()

    def run_locked_holdout(
        self,
        initial_squad: SquadState,
        simulations: int = 500,
        seed: int = 42,
    ) -> dict[str, float]:
        """Run a historical backtest of a strategy specifically on 2025-26 locked holdout."""
        if initial_squad.season != "2025-26":
            raise ValueError("Locked holdout can only run on 2025-26 season.")
        return self.backtest_strategy(
            "holdout", 1, 38, initial_squad, simulations=simulations, seed=seed
        )

    # ------------------------------------------------------------------
    # Actual-value lookups (strictly separated: used only to score outcomes)
    # ------------------------------------------------------------------

    def _season_id(self, season: str) -> int:
        from sqlalchemy import select
        from fpl_intelligence.db.models import Season
        assert self.db is not None
        season_row = self.db.scalar(select(Season).where(Season.code == season))
        if season_row is None:
            raise ValueError(
                f"Season '{season}' not present in database. Populate real data "
                "before running a decision backtest."
            )
        return season_row.id

    def _actual_points(self, season_id: int, gw_num: int) -> dict[int, float]:
        """Return {player_id: actual total_points} for a gameweek in a season.

        Uses ``Gameweek.provider_event_id`` (the FPL gameweek number) to match,
        since the schema stores the GW number there, not in a ``number`` column.
        """
        from sqlalchemy import select
        from fpl_intelligence.db.models import Gameweek, PlayerGameweekPerformance
        assert self.db is not None
        gw = self.db.scalar(
            select(Gameweek).where(
                Gameweek.season_id == season_id, Gameweek.provider_event_id == gw_num
            )
        )
        if gw is None:
            return {}
        perfs = list(
            self.db.execute(
                select(PlayerGameweekPerformance).where(
                    PlayerGameweekPerformance.gameweek_id == gw.id
                )
            ).scalars().all()
        )
        return {p.player_id: float(p.total_points or 0) for p in perfs}

    def _player_positions(self, season_id: int) -> dict[int, int]:
        """Return {player_id: position_code} from the Player table.

        Position is a fixed player attribute (not season-specific in the schema),
        so we read it directly from ``Player.position_code``.
        """
        from sqlalchemy import select
        from fpl_intelligence.db.models import Player
        assert self.db is not None
        rows = list(
            self.db.execute(
                select(Player.id, Player.position_code)
            ).all()
        )
        return {pid: pos for pid, pos in rows if pos is not None}

    @staticmethod
    def _is_valid_formation(positions: list[int]) -> bool:
        from collections import Counter
        counts = Counter(positions)
        limits = {1: (1, 1), 2: (3, 5), 3: (2, 5), 4: (1, 3)}
        if len(positions) != 11:
            return False
        for pos, (lo, hi) in limits.items():
            if not (lo <= counts.get(pos, 0) <= hi):
                return False
        return True

    def _select_starting_xi(
        self, pids: list[int], positions: dict[int, int], evs: dict[int, float]
    ) -> list[int]:
        """Greedy formation-valid starting XI selection by EV."""
        from itertools import combinations
        ordered = sorted(pids, key=lambda p: evs.get(p, 0.0), reverse=True)
        pool = ordered[:13]
        best = None
        best_ev = -1.0
        for combo in combinations(pool, 11):
            poss = [positions.get(p) for p in combo]
            if None in poss:
                continue
            if not self._is_valid_formation(list(poss)):  # type: ignore[arg-type]
                continue
            ev = sum(evs.get(p, 0.0) for p in combo)
            if ev > best_ev:
                best_ev = ev
                best = combo
        return list(best) if best is not None else ordered[:11]

    def backtest_strategy(
        self,
        strategy_name: str,
        start_gw: int,
        end_gw: int,
        initial_squad: SquadState,
        objective: DecisionObjective = DecisionObjective.MAXIMIZE_GW_POINTS,
        simulations: int = 500,
        seed: int = 42,
    ) -> dict[str, float]:
        """Run a historical backtest of a strategy over a sequence of gameweeks.

        Uses the populated database for actual gameweek points and the provider
        for predictive distributions. Raises if the database is unavailable or
        empty for the target season (no fabricated results).
        """
        if self.db is None:
            raise RuntimeError(
                "DecisionBacktester.backtest_strategy requires a populated "
                "database session (db is None). Real-data backtests cannot run "
                "without one."
            )
        season = initial_squad.season
        season_id = self._season_id(season)
        positions = self._player_positions(season_id)

        squad_players = list(initial_squad.squad_players)
        free_transfers = initial_squad.free_transfers

        transfer_costs = 0.0
        transfer_events = 0
        total_points = 0.0
        captain_points = 0.0
        gw_count = 0
        gw_scores: list[float] = []

        for gw in range(start_gw, end_gw + 1):
            actuals = self._actual_points(season_id, gw)
            if not actuals:
                # Gameweek not present (e.g. truncated season) -> stop walking.
                break

            # 1. Predictions for the current squad + wider transfer pool.
            squad_evs: dict[int, float] = {}
            for pid in squad_players:
                pred = self.provider.get_player_prediction(pid, gw)
                squad_evs[pid] = pred.expected_points

            pool_evs: dict[int, float] = {}
            try:
                all_preds = self.provider.get_all_predictions(gw)
                for pid, pred in all_preds.items():
                    pool_evs[pid] = pred.expected_points
            except Exception:  # noqa: BLE001
                pool_evs = dict(squad_evs)

            # 2. Decision: roll vs transfer. Find best upgrade for a weak link.
            weakest = sorted(squad_players, key=lambda p: squad_evs.get(p, 0.0))[:3]
            chosen_out = None
            chosen_in = None
            best_gain = 0.0
            for p_out in weakest:
                pos_out = positions.get(p_out)
                ranked = sorted(pool_evs.items(), key=lambda kv: kv[1], reverse=True)
                for p_in, ev_in in ranked:
                    if p_in in squad_players:
                        continue
                    if positions.get(p_in) != pos_out:
                        continue
                    gain = ev_in - squad_evs.get(p_out, 0.0)
                    if gain > best_gain:
                        best_gain = gain
                        chosen_out = p_out
                        chosen_in = p_in
                    break  # only the top target per position matters here
                if chosen_in is not None and best_gain > 2.0:
                    break

            if chosen_in is not None and chosen_out is not None and best_gain > 2.0:
                if free_transfers < 1:
                    transfer_costs += float(self.rules.transfer_hit_cost)
                else:
                    free_transfers -= 1
                squad_players.remove(chosen_out)
                squad_players.append(chosen_in)
                squad_evs[chosen_in] = pool_evs.get(chosen_in, 0.0)
                transfer_events += 1

            # 3. Starting XI + captain by EV (prediction only, no look-ahead).
            xi = self._select_starting_xi(squad_players, positions, squad_evs)
            captain = max(xi, key=lambda p: squad_evs.get(p, 0.0))

            # 4. Credit actual historical points (scoring only, never features).
            gw_points = sum(actuals.get(pid, 0.0) for pid in xi)
            captain_actual = actuals.get(captain, 0.0)
            captain_points += captain_actual
            total_points += gw_points + captain_actual  # double the captain

            gw_scores.append(gw_points + captain_actual)
            gw_count += 1

        if gw_count == 0:
            raise ValueError(
                f"No playable gameweeks found for season '{season}'. "
                "Cannot produce backtest metrics from an empty dataset."
            )

        avg = total_points / gw_count
        return {
            "total_points": round(total_points, 2),
            "gw_average": round(avg, 2),
            "gw_count": float(gw_count),
            "transfer_events": float(transfer_events),
            "transfer_costs": round(transfer_costs, 2),
            "hit_roi": round(-transfer_costs / gw_count, 2),
            "captain_points": round(captain_points, 2),
            "captain_extra": round(captain_points, 2),
            "downside_variance": round(float(np.var(gw_scores)), 2),
        }

