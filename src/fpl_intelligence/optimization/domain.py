"""Domain models for decision optimization."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DecisionObjective(StrEnum):
    """Explicit optimization objectives."""

    MAXIMIZE_GW_POINTS = "maximize_gw_points"
    MAXIMIZE_MULTI_GW_POINTS = "maximize_multi_gw_points"
    RISK_ADJUSTED_POINTS = "risk_adjusted_points"
    CHASE_RANK = "chase_rank"
    PROTECT_RANK = "protect_rank"


class ActionType(StrEnum):
    """Types of actions."""

    ROLL = "roll"
    TRANSFER = "transfer"
    WILDCARD = "wildcard"
    FREE_HIT = "free_hit"
    BENCH_BOOST = "bench_boost"
    TRIPLE_CAPTAIN = "triple_captain"
    STARTING_XI = "starting_xi"
    CAPTAIN = "captain"


@dataclass
class SquadState:
    """Canonical representation of a user's FPL squad state."""

    manager_id: int
    season: str
    gameweek: int
    squad_players: list[int]  # List of 15 player IDs
    starting_xi: list[int]  # List of 11 player IDs
    bench_order: list[int]  # List of 4 player IDs (usually 1 GK, 3 outfield)
    captain: int
    vice_captain: int
    bank: float  # In millions (e.g., 1.5)
    team_value: float
    free_transfers: int
    rolled_transfers: int
    transfer_hits: int
    active_chips: list[str] = field(default_factory=list)
    remaining_chips: list[str] = field(
        default_factory=lambda: [
            "wildcard_1",
            "wildcard_2",
            "free_hit",
            "bench_boost",
            "triple_captain",
        ]
    )
    transfer_history: list[dict[str, Any]] = field(default_factory=list)
    team_value_history: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manager_id": self.manager_id,
            "season": self.season,
            "gameweek": self.gameweek,
            "squad_players": self.squad_players,
            "starting_xi": self.starting_xi,
            "bench_order": self.bench_order,
            "captain": self.captain,
            "vice_captain": self.vice_captain,
            "bank": self.bank,
            "team_value": self.team_value,
            "free_transfers": self.free_transfers,
            "rolled_transfers": self.rolled_transfers,
            "transfer_hits": self.transfer_hits,
            "active_chips": self.active_chips,
            "remaining_chips": self.remaining_chips,
        }


@dataclass
class CandidateAction:
    """A candidate action to be evaluated by the optimizer."""

    action_type: ActionType
    transfers_in: list[int] = field(default_factory=list)
    transfers_out: list[int] = field(default_factory=list)
    hit_cost: int = 0
    chip: str | None = None
    target_gameweek: int | None = None
    horizon: int = 1  # Number of gameweeks to evaluate

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "transfers_in": self.transfers_in,
            "transfers_out": self.transfers_out,
            "hit_cost": self.hit_cost,
            "chip": self.chip,
            "target_gameweek": self.target_gameweek,
            "horizon": self.horizon,
        }


@dataclass
class Recommendation:
    """An evaluated recommendation with confidence intervals."""

    action: CandidateAction
    expected_gain: float
    base_case: float
    downside_case: float  # P10
    upside_case: float  # P90
    probability_positive: float
    confidence: float  # Decision confidence (0-1)
    main_reason: str = ""
    main_risk: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.to_dict(),
            "expected_gain": round(self.expected_gain, 4),
            "base_case": round(self.base_case, 4),
            "downside_case": round(self.downside_case, 4),
            "upside_case": round(self.upside_case, 4),
            "probability_positive": round(self.probability_positive, 4),
            "confidence": round(self.confidence, 4),
            "main_reason": self.main_reason,
            "main_risk": self.main_risk,
        }
