"""Request-local full-player-pool caching for the live prediction provider."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from fpl_intelligence.data_providers.registry import ProviderRegistry
from fpl_intelligence.optimization.provider import PlayerPrediction
from fpl_intelligence.prediction.live_provider import LivePredictionProvider


class CachedLivePredictionProvider(LivePredictionProvider):
    """Live provider with a request-local cache for full gameweek pools.

    Chip simulations can request the same full player universe more than once,
    especially across repeated Free Hit/Wildcard evaluations. Caching the final
    labelled mapping avoids repeating the full-universe iteration and NumPy
    prediction materialization while keeping cache lifetime scoped to this
    provider instance (one API request).
    """

    def __init__(
        self,
        session: Session,
        *,
        catalog_path: Path | None = None,
        understat_snapshot_path: Path | None = None,
        provider_registry: ProviderRegistry | None = None,
    ) -> None:
        super().__init__(
            session,
            catalog_path=catalog_path,
            understat_snapshot_path=understat_snapshot_path,
            provider_registry=provider_registry,
        )
        self._all_predictions_cache: dict[
            tuple[int, bool], dict[int, PlayerPrediction]
        ] = {}

    def get_all_predictions(
        self, gameweek: int, *, skip_materialized: bool = False
    ) -> dict[int, PlayerPrediction]:
        """Return the cached full pool for this request/gameweek when available."""
        cache_key = (int(gameweek), bool(skip_materialized))
        cached = self._all_predictions_cache.get(cache_key)
        if cached is not None:
            return cached

        predictions = super().get_all_predictions(
            int(gameweek),
            skip_materialized=skip_materialized,
        )
        self._all_predictions_cache[cache_key] = predictions
        return predictions
