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

    v2.7.10 — bulk-pool promotion: after ``get_all_predictions`` populates the
    full universe for a gameweek, individual ``get_player_prediction`` calls for
    players already in that pool are served directly from the bulk cache rather
    than re-running ``resolve_chain`` + ``_label_predictions``.  This removes
    the dominant N×M redundancy across the four optimizer stages
    (StartingXI → Captain → MultiTransferPlanner → ChipSimulator) when each
    stage iterates over the same squad players for the same gameweek.

    Optimizer timings are accumulated in ``stage_timings`` (ms wall-clock per
    stage name) and cleared at the start of each ``generate_decisions()`` call
    via :meth:`clear_request_cache`.
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
        self._all_predictions_cache: dict[tuple[int, bool], dict[int, PlayerPrediction]] = {}
        self._fixture_count_cache: dict[tuple[int, int], int] = {}
        # Fine-grained optimizer timing accumulator: {stage_name: elapsed_ms}
        self.stage_timings: dict[str, float] = {}

    # -- Cache lifecycle --------------------------------------------------------

    def clear_request_cache(self) -> None:
        """Reset all per-request caches. Call at the top of generate_decisions()."""
        self._all_predictions_cache.clear()
        self._fixture_count_cache.clear()
        self._chain_cache.clear()
        self._prediction_cache.clear()
        self.stage_timings.clear()

    # -- Timing helper ----------------------------------------------------------

    def record_stage_timing(self, stage: str, elapsed_ms: float) -> None:
        """Accumulate elapsed time for a named optimizer stage."""
        self.stage_timings[stage] = self.stage_timings.get(stage, 0.0) + elapsed_ms

    # -- Cached prediction access -----------------------------------------------

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

    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        """Serve one player/gameweek pair — promotes from bulk cache when available.

        If the full pool for ``gameweek`` was already materialised by a prior
        ``get_all_predictions`` call (e.g. from the chip simulator), the result
        is returned directly rather than re-running the chain.  Falls back to
        the parent ``get_player_prediction`` (which uses ``_prediction_cache``)
        when the bulk pool hasn't been populated yet.
        """
        pid = int(player_id)
        gw = int(gameweek)

        # Fast path: check if the bulk pool has already been populated.
        for skip_mat in (False, True):
            pool = self._all_predictions_cache.get((gw, skip_mat))
            if pool is not None:
                pred = pool.get(pid)
                if pred is not None:
                    return pred
                # Player absent from bulk pool — fall through to per-player path.
                break

        return super().get_player_prediction(pid, gw)

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        """Return cached fixture count for this request when available.

        The Phase 6 decision bridge wraps this provider in ``_TimedPredictionProvider``
        which has its own request-local cache for ``get_fixture_count``. Direct
        callers (e.g. Phase 9.4 ``PredictionContextBuilder``) skip the bridge and
        hit this provider directly. Caching here removes duplicate
        ``gameweek + membership + fixtures`` query sequences for any caller
        within the same request, regardless of whether the caller goes through
        the bridge or the raw provider.
        """
        cache_key = (int(player_id), int(gameweek))
        cached = self._fixture_count_cache.get(cache_key)
        if cached is not None:
            return cached

        count = super().get_fixture_count(int(player_id), int(gameweek))
        self._fixture_count_cache[cache_key] = int(count)
        return int(count)
