"""Phase 11.1 — Bridge from live structured facts to the decision engine.

Two pieces:

* :class:`FactOverrideProvider` — a :class:`DecisionPredictionProvider` that
  wraps the baseline quantitative provider (Phases 4/5) and applies
  :class:`FactOverride` objects post-hoc. It does **not** mutate any Phase 1–8
  model: it asks the baseline provider for a prediction and then adjusts only
  the public prediction fields (``start_probability``, ``expected_minutes``,
  and a proportional rescale of ``expected_points``). This keeps the core
  quantitative algorithms untouched while still letting hard facts win.
* :class:`FactCollectionService` — wires the three connectors + the
  :class:`LiveFactInjector` together. ``collect_overrides`` is cache-first and
  isolates each source, so a missing key or a network failure degrades to fewer
  facts rather than an error.

The FastAPI ``GET /api/v1/decisions`` endpoint uses these: when live facts are
requested it calls :meth:`FactCollectionService.collect_overrides`, wraps the
provider, and otherwise falls back to the baseline provider unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from fpl_intelligence.data_providers.api_football import ApiFootballConnector
from fpl_intelligence.data_providers.base import DataConnectorError
from fpl_intelligence.data_providers.cache import ResponseCache
from fpl_intelligence.data_providers.facts import FactOverride
from fpl_intelligence.data_providers.football_data_org import FootballDataOrgConnector
from fpl_intelligence.data_providers.fpl_official import FplOfficialConnector
from fpl_intelligence.data_providers.live_fact_injector import (
    LiveFactInjector,
    LiveFactResult,
)
from fpl_intelligence.data_providers.registry import ProviderRegistry, build_default_registry
from fpl_intelligence.optimization.provider import (
    DecisionPredictionProvider,
    PlayerPrediction,
)


class FactOverrideProvider(DecisionPredictionProvider):
    """Apply fact overrides on top of a baseline quantitative provider.

    Each prediction is fetched from the wrapped provider and then adjusted
    according to the override for that player (if any). Fields left as ``None``
    in the override keep their baseline value.
    """

    def __init__(
        self,
        base: DecisionPredictionProvider,
        overrides: Mapping[int, FactOverride] | list[FactOverride],
    ) -> None:
        self._base = base
        if isinstance(overrides, list):
            self._overrides = {o.player_id: o for o in overrides}
        else:
            self._overrides = dict(overrides)

    def _apply(self, override: FactOverride, pred: PlayerPrediction) -> PlayerPrediction:
        start = pred.start_probability
        minutes = pred.expected_minutes
        if override.start_probability is not None:
            start = float(override.start_probability)
        if override.expected_minutes is not None:
            minutes = float(override.expected_minutes)

        points = pred.expected_points
        if pred.start_probability > 0:
            points *= start / pred.start_probability
        if pred.expected_minutes > 0:
            points *= minutes / pred.expected_minutes
        points = max(0.0, points)

        # Rescale the distribution + bounds too. The Phase 6 optimizers read
        # ``distribution``'s mean (not ``expected_points``), so a hard fact only
        # actually wins if we rescale the whole predictive object. We build a new
        # array (never mutate the upstream model's data).
        factor = (points / pred.expected_points) if pred.expected_points > 0 else 0.0
        dist = pred.distribution
        if factor == 1.0:
            new_dist = dist
        elif factor == 0.0:
            new_dist = np.zeros_like(dist) if dist is not None else None
        else:
            new_dist = dist * factor if dist is not None else None
        floor = pred.floor * factor if pred.floor > 0 else pred.floor
        ceiling = pred.ceiling * factor if pred.ceiling > 0 else pred.ceiling

        return PlayerPrediction(
            player_id=pred.player_id,
            gameweek=pred.gameweek,
            expected_points=points,
            expected_minutes=minutes,
            start_probability=start,
            distribution=new_dist,
            floor=floor,
            ceiling=ceiling,
            confidence=pred.confidence,
            data_completeness=pred.data_completeness,
        )

    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        pred = self._base.get_player_prediction(player_id, gameweek)
        override = self._overrides.get(player_id)
        if override is None:
            return pred
        return self._apply(override, pred)

    def get_squad_predictions(
        self, squad_players: list[int], gameweeks: list[int]
    ) -> dict[int, dict[int, PlayerPrediction]]:
        base = self._base.get_squad_predictions(squad_players, gameweeks)
        out: dict[int, dict[int, PlayerPrediction]] = {}
        for gw, by_player in base.items():
            out[gw] = {pid: self.get_player_prediction(pid, gw) for pid in by_player}
        return out

    def get_all_predictions(self, gameweek: int) -> dict[int, PlayerPrediction]:
        base = self._base.get_all_predictions(gameweek)
        return {pid: self.get_player_prediction(pid, gameweek) for pid in base}

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        return self._base.get_fixture_count(player_id, gameweek)


class FactCollectionService:
    """Orchestrate the three connectors + injector into fact overrides."""

    def __init__(
        self,
        *,
        cache: ResponseCache | None = None,
        http_client: Any = None,
        fpl_connector: FplOfficialConnector | None = None,
        api_football_connector: ApiFootballConnector | None = None,
        football_data_connector: FootballDataOrgConnector | None = None,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self._cache = cache or ResponseCache()
        self._http_client = http_client
        fpl = fpl_connector or FplOfficialConnector(
            cache=self._cache, http_client=self._http_client
        )
        api = api_football_connector or ApiFootballConnector(
            cache=self._cache, http_client=self._http_client
        )
        self._registry = registry or build_default_registry(fpl=fpl, api_football=api)
        self._fd = football_data_connector

    def _build_fpl(self) -> FplOfficialConnector:
        return self._registry.provider("fpl_official")

    def _build_api_football(self) -> ApiFootballConnector:
        return self._registry.provider("api_football")

    def _build_football_data(self) -> FootballDataOrgConnector:
        return self._fd or FootballDataOrgConnector(
            cache=self._cache, http_client=self._http_client
        )

    def collect_overrides(
        self,
        *,
        date: str | None = None,
        season: int | None = None,
        fpl_id_map: Mapping[int, int] | None = None,
    ) -> LiveFactResult:
        """Fetch facts from all enabled connectors and build overrides.

        Any connector failure is caught and recorded in the result diagnostics;
        the call never raises for an individual source's failure (a network
        outage simply yields fewer facts).
        """
        injector = LiveFactInjector()
        try:
            return injector.inject_from_connectors(
                fpl=self._build_fpl(),
                api_football=self._build_api_football(),
                football_data=self._build_football_data(),
                date=date,
                season=season,
                fpl_id_map=fpl_id_map,
            )
        except DataConnectorError as exc:  # pragma: no cover - defensive
            return LiveFactResult(
                diagnostics={"error": str(exc), "fpl_official": {"enabled": True}}
            )


def index_overrides(overrides: list[FactOverride]) -> dict[int, FactOverride]:
    """Helper: build a player_id -> override index for a provider wrapper."""
    return {o.player_id: o for o in overrides}
