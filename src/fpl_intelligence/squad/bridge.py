"""Bridge between the user's squad state and the Phase 6 Decision Optimization Engine."""

from __future__ import annotations

from fpl_intelligence.optimization.chips import ChipSimulator
from fpl_intelligence.optimization.domain import SquadState
from fpl_intelligence.optimization.provider import DecisionPredictionProvider
from fpl_intelligence.optimization.rules import FPLRules
from fpl_intelligence.optimization.squad import CaptainOptimizer, StartingXIOptimizer
from fpl_intelligence.optimization.transfers import (
    MultiTransferPlanner,
    TransferOptimizer,
)
from fpl_intelligence.squad.models import (
    CaptainRecommendation,
    ChipRecommendation,
    DecisionReport,
    SquadStateCreate,
    TransferPlan,
)


class DecisionOptimizerBridge:
    """Connects a user's squad state to the Phase 6 optimizers.

    Accepts a :class:`SquadStateCreate` and a gameweek, then delegates to
    the canonical Phase 6 optimizer classes to produce a structured
    :class:`DecisionReport`.

    Auxiliary metadata (``player_positions``, ``player_prices``,
    ``player_teams``) is optional but required for full optimization
    coverage.  When missing, the bridge degrades gracefully: starting-XI
    and captain recommendations are still produced (using a naive
    first-11 fallback), while transfer and chip plans are omitted.
    """

    def __init__(
        self,
        provider: DecisionPredictionProvider,
        rules: FPLRules | None = None,
    ) -> None:
        self.provider = provider
        self.rules = rules or FPLRules()
        self._starting_xi_opt = StartingXIOptimizer(provider, self.rules)
        self._captain_opt = CaptainOptimizer(provider)
        self._transfer_opt = TransferOptimizer(provider, self.rules)
        self._multi_transfer = MultiTransferPlanner(self._transfer_opt, provider, self.rules)
        self._chip_sim = ChipSimulator(provider, self.rules)

    def generate_decisions(
        self,
        squad: SquadStateCreate,
    ) -> DecisionReport:
        """Generate a full :class:`DecisionReport` for the supplied squad.

        Args:
            squad: The user's current squad state plus optional metadata.

        Returns:
            A :class:`DecisionReport` with optimized starting XI, bench
            order, captain, transfer plan, and chip recommendation.
        """
        gw = squad.gameweek
        player_positions = squad.player_positions or {}
        player_prices = squad.player_prices or {}
        player_teams = squad.player_teams or {}

        opt_squad = self._to_domain_squad(squad)

        starting_xi, bench_order = self._optimize_xi(opt_squad, gw, player_positions)
        opt_squad.starting_xi = starting_xi
        opt_squad.bench_order = bench_order

        captain_rec = self._recommend_captain(opt_squad, gw)
        transfer_plan = self._plan_transfers(
            opt_squad, gw, player_positions, player_prices, player_teams
        )
        chip_rec = self._recommend_chip(opt_squad, gw)

        return DecisionReport(
            gameweek=gw,
            starting_xi=starting_xi,
            bench_order=bench_order,
            captain=captain_rec,
            vice_captain=opt_squad.vice_captain,
            transfer_plan=transfer_plan,
            chip_recommendation=chip_rec,
        )

    def _to_domain_squad(self, squad: SquadStateCreate) -> SquadState:
        """Convert the API-level squad payload to the optimization-domain SquadState."""
        prices = squad.player_prices or {}
        squad_value = sum(prices.get(pid, 8.0) for pid in squad.player_ids)
        remaining_chips = [
            c for c in squad.chips_available if c in self.rules.rules.get("chips", {})
        ]
        if not remaining_chips:
            remaining_chips = list(squad.chips_available)

        return SquadState(
            manager_id=1,
            season="2025-26",
            gameweek=squad.gameweek,
            squad_players=list(squad.player_ids),
            starting_xi=[],
            bench_order=[],
            captain=squad.captain_id,
            vice_captain=squad.vice_captain_id,
            bank=float(squad.bank),
            team_value=float(squad_value + squad.bank),
            free_transfers=squad.free_transfers,
            rolled_transfers=0,
            transfer_hits=0,
            remaining_chips=remaining_chips,
            active_chips=[],
            transfer_history=[],
            team_value_history=[],
        )

    def _optimize_xi(
        self,
        squad: SquadState,
        gw: int,
        player_positions: dict[int, int],
    ) -> tuple[list[int], list[int]]:
        """Run the StartingXI optimizer or return a naive fallback."""
        if not player_positions:
            naive_xi = squad.squad_players[: self.rules.starting_xi_size]
            bench = [pid for pid in squad.squad_players if pid not in naive_xi]
            return naive_xi, bench

        xi, bench = self._starting_xi_opt.optimize_xi(
            squad_players=squad.squad_players,
            gameweek=gw,
            player_positions=player_positions,
        )
        return xi, bench

    def _recommend_captain(
        self,
        squad: SquadState,
        gw: int,
    ) -> CaptainRecommendation | None:
        """Run the captain optimizer and translate the result."""
        if not squad.starting_xi:
            return None

        rec = self._captain_opt.recommend_captain(squad)
        return CaptainRecommendation(
            player_id=rec.action.transfers_in[0] if rec.action.transfers_in else squad.captain,
            expected_points=rec.expected_gain,
            expected_gain=rec.expected_gain,
            probability_positive=rec.probability_positive,
            confidence=rec.confidence,
            main_reason=rec.main_reason,
            main_risk=rec.main_risk,
        )

    def _plan_transfers(
        self,
        squad: SquadState,
        gw: int,
        player_positions: dict[int, int],
        player_prices: dict[int, float],
        player_teams: dict[int, int],
    ) -> TransferPlan | None:
        """Run the multi-transfer planner or return None when metadata is missing."""
        if not player_positions or not player_prices or not player_teams:
            return None

        rec = self._multi_transfer.generate_candidates(
            squad=squad,
            player_positions=player_positions,
            player_prices=player_prices,
            player_teams=player_teams,
        )
        action = rec.action
        return TransferPlan(
            action_type=action.action_type.value,
            transfers_in=list(action.transfers_in),
            transfers_out=list(action.transfers_out),
            hit_cost=action.hit_cost,
            expected_gain=rec.expected_gain,
            probability_positive=rec.probability_positive,
            confidence=rec.confidence,
            main_reason=rec.main_reason,
            main_risk=rec.main_risk,
        )

    def _recommend_chip(
        self,
        squad: SquadState,
        gw: int,
    ) -> ChipRecommendation | None:
        """Run the chip simulator and translate the result."""
        rec = self._chip_sim.recommend_chip(squad, gw)
        if rec is None:
            return None

        chip_name = rec.action.chip if rec.action.chip else rec.action.action_type.value
        return ChipRecommendation(
            chip_name=chip_name,
            expected_gain=rec.expected_gain,
            confidence=rec.confidence,
            main_reason=rec.main_reason,
        )
