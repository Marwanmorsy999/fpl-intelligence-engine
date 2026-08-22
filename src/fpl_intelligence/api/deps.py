"""Phase 10.1 — FastAPI dependency-injection providers.

Centralises every external seam the intelligence API touches so that:

* the database session, the LLM provider, the quantitative prediction provider,
  and the Phase 9.4 bridge objects are all injected via ``Depends``;
* the LLM provider **defaults to** :class:`MockLLMProvider` so a stray test or
  forgotten flag can never silently drain a real-provider quota;
* a real provider is only ever built when the caller opts in through the
  ``FPL_API_USE_LIVE_LLM=true`` environment variable *or* the
  ``X-FPL-LLM-Mode: live`` request header — and the construction reads
  credentials from configuration/environment, never from source.
"""
from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from fpl_intelligence.config import get_settings
from fpl_intelligence.db.session import get_db as _get_db_session
from fpl_intelligence.live_intelligence.analyst import AIAnalyst
from fpl_intelligence.live_intelligence.bridge import (
    EvidenceQueryService,
    PredictionContextBuilder,
)
from fpl_intelligence.live_intelligence.extraction import LLMProvider
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider
from fpl_intelligence.optimization.provider import DecisionPredictionProvider

#: Opt-in switch for real LLM calls. Defaults to the safe (mock) path.
_LIVE_LLM_ENV = "FPL_API_USE_LIVE_LLM"

GetDB = Annotated[Session, Depends(_get_db_session)]


def get_llm_provider(x_fpl_llm_mode: Annotated[str | None, Header()] = None) -> LLMProvider:
    """Return the LLM provider for the request.

    Mock by default. A real provider is built only when the caller explicitly
    opts in via ``FPL_API_USE_LIVE_LLM=true`` *or* the ``X-FPL-LLM-Mode: live``
    header. Real construction reads credentials from configuration/environment.
    """
    use_live = os.getenv(_LIVE_LLM_ENV, "false").strip().lower() == "true"
    if x_fpl_llm_mode is not None and x_fpl_llm_mode.strip().lower() == "live":
        use_live = True
    if not use_live:
        return MockLLMProvider()

    from fpl_intelligence.live_intelligence.llm_providers import ProviderFactory
    from fpl_intelligence.live_intelligence.llm_settings import (
        LLMSettingsError,
        load_llm_settings,
    )

    try:
        settings = load_llm_settings()
        return ProviderFactory(settings).create(None)
    except LLMSettingsError as exc:  # pragma: no cover - depends on deploy env
        raise HTTPException(
            status_code=503,
            detail=f"Live LLM provider requested but unavailable: {exc}",
        ) from exc


def assert_no_static_stub_in_production() -> None:
    """Fail fast when a production deployment would serve the hardcoded stub.

    Called once at import time by :mod:`fpl_intelligence.api.main`. A production
    environment resolving :class:`StaticPredictionProvider` means every user
    sees fake 5.5-point predictions — exactly the Phase 15.0 defect this guard
    exists to prevent. Set ``PREDICTION_PROVIDER=live`` (or unset it) to fix.
    """
    settings = get_settings()
    if (
        settings.app_env.strip().lower() == "production"
        and settings.prediction_provider.strip().lower() == "static"
    ):
        raise RuntimeError(
            "PREDICTION_PROVIDER=static is forbidden in APP_ENV=production: "
            "it serves the hardcoded StaticPredictionProvider stub (5.5 xPTS "
            "for everyone). Use PREDICTION_PROVIDER=live."
        )


def get_prediction_provider(db: GetDB) -> DecisionPredictionProvider:
    """Return the quantitative prediction provider (Phase 15.0 chain).

    * ``PREDICTION_PROVIDER=live`` (default) resolves the
      :class:`~fpl_intelligence.prediction.live_provider.LivePredictionProvider`
      whose documented fallback chain is:

      1. ``model-backtest``       — latest successful backtest's player_predictions.
      2. ``baseline-model``       — Phase 5 baselines over ingested gameweek features.
      3. ``pre-season-proxy-v2``  — price percentile x FDR x Understat xG/xA
                                    (offline snapshot) x market implied probs.
      4. ``static-stub``          — ONLY when explicitly configured for tests.

      Every prediction carries its ``source`` and ``data_quality`` label so the
      UI can never present a proxy as computed form.
    * ``PREDICTION_PROVIDER=static`` resolves the deterministic offline stub —
      unit tests and ``--dry-run`` only. Production startup refuses it (see
      :func:`assert_no_static_stub_in_production`).
    """
    settings = get_settings()
    if settings.prediction_provider.strip().lower() == "static":
        from fpl_intelligence.live_intelligence.bridge import StaticPredictionProvider

        return StaticPredictionProvider()

    from fpl_intelligence.prediction.live_provider import LivePredictionProvider

    return LivePredictionProvider(session=db)



def get_prediction_builder(
    db: GetDB,
    prediction_provider: Annotated[
        DecisionPredictionProvider, Depends(get_prediction_provider)
    ],
) -> PredictionContextBuilder:
    """Inject the Phase 9.4 quantitative bridge."""
    _ = db  # session kept for parity with the evidence service; builder is read-only
    return PredictionContextBuilder(prediction_provider=prediction_provider)


def get_evidence_service(db: GetDB) -> EvidenceQueryService:
    """Inject the Phase 9.4 evidence query service (real evidence only)."""
    return EvidenceQueryService(db)


def get_analyst(
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
) -> AIAnalyst:
    """Inject the Phase 9.3 AI Analyst."""
    return AIAnalyst(provider)
