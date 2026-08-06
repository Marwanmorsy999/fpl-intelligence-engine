"""Configuration for FPL rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml  # type: ignore[import-untyped]

DEFAULT_FPL_RULES: dict[str, Any] = {
    "rules_version": "default-official",
    "squad_size": 15,
    "starting_xi_size": 11,
    "budget_millions": 100.0,
    "max_players_per_club": 3,
    "position_limits": {
        1: 2,  # GK
        2: 5,  # DEF
        3: 5,  # MID
        4: 3,  # FWD
    },
    "formation_limits": {
        1: {"min": 1, "max": 1},  # GK
        2: {"min": 3, "max": 5},  # DEF
        3: {"min": 2, "max": 5},  # MID
        4: {"min": 1, "max": 3},  # FWD
    },
    "transfer_hit_cost": 4,
    "max_rolled_transfers": 5,  # Modern rules allow up to 5 banked transfers
    "chips": {
        "wildcard": {"count": 2},
        "free_hit": {"count": 1},
        "triple_captain": {"count": 1},
        "bench_boost": {"count": 1},
    },
    "chips_per_half": False,
}

RULES_2026_27: dict[str, Any] = {
    **DEFAULT_FPL_RULES,
    "rules_version": "2026_27",
    "chips_per_half": True,
    "chips": {
        "wildcard": {"count": 2},  # 1 per half
        "free_hit": {"count": 2},  # 1 per half
        "triple_captain": {"count": 2},  # 1 per half
        "bench_boost": {"count": 2},  # 1 per half
    },
}

@dataclass
class FPLRules:
    """FPL Rules configuration object."""

    rules: dict[str, Any] = field(default_factory=lambda: DEFAULT_FPL_RULES)

    @property
    def rules_version(self) -> str:
        return str(self.rules.get("rules_version", "unknown"))

    @property
    def squad_size(self) -> int:
        return int(self.rules.get("squad_size", 15))

    @property
    def starting_xi_size(self) -> int:
        return int(self.rules.get("starting_xi_size", 11))

    @property
    def budget_millions(self) -> float:
        return float(self.rules.get("budget_millions", 100.0))

    @property
    def max_players_per_club(self) -> int:
        return int(self.rules.get("max_players_per_club", 3))

    def position_limit(self, position_code: int) -> int:
        """Maximum number of players allowed for a given position."""
        limits = self.rules.get("position_limits", {})
        return int(limits.get(position_code, 0))

    def min_formation(self, position_code: int) -> int:
        """Minimum number of players required to start in this position."""
        limits = self.rules.get("formation_limits", {})
        pos_limits = limits.get(position_code, {})
        return int(pos_limits.get("min", 0))

    def max_formation(self, position_code: int) -> int:
        """Maximum number of players allowed to start in this position."""
        limits = self.rules.get("formation_limits", {})
        pos_limits = limits.get(position_code, {})
        return int(pos_limits.get("max", 0))

    @property
    def transfer_hit_cost(self) -> int:
        return int(self.rules.get("transfer_hit_cost", 4))

    @property
    def max_rolled_transfers(self) -> int:
        return int(self.rules.get("max_rolled_transfers", 5))
        
    @property
    def is_half_season_chips(self) -> bool:
        """Returns True if the 2026/27 chip rules apply (1 set per half)."""
        return bool(self.rules.get("chips_per_half", False))

    def get_chip_count(self, chip_name: str) -> int:
        chips = self.rules.get("chips", {})
        chip_info = chips.get(chip_name, {})
        return int(chip_info.get("count", 0))
        
    def get_half_season(self, gameweek: int) -> int:
        """Returns 1 for GW 1-19, 2 for GW 20-38."""
        return 1 if gameweek <= 19 else 2

    @classmethod
    def load_yaml(cls, path: str) -> FPLRules:
        """Load rules from a YAML file."""
        with open(path, encoding="utf-8") as f:
            rules = yaml.safe_load(f)
        return cls(rules)
