"""Phase 20.1 — materialization layer.

The production incident root cause was *computing per request*: every page
triggered live egress fetches (FPL API is blocked from Vercel datacenter IPs,
so each one hung until timeout) and re-ran the prediction chain inline.

This package flips the architecture: a single daily cron fetches everything
once from sources Vercel CAN reach (vaastav raw.githubusercontent, BBC RSS),
writes materialized tables, and precomputes per-player xPTS for the next five
gameweeks. Request paths then read only indexed tables.
"""

from fpl_intelligence.materialize.service import (
    load_cached_fixtures,
    load_cached_news_items,
    materialize_all,
    team_names_from_db,
)

__all__ = [
    "materialize_all",
    "load_cached_fixtures",
    "load_cached_news_items",
    "team_names_from_db",
]
