"""Phase 7 minutes-model integration.

Wraps the base :class:`MinutesModel` with availability-aware adjustment.

Design principle (Part 15 — Model Integration):
- Do NOT directly overwrite model probabilities with arbitrary fixed numbers.
- Availability state adjusts the model output via confidence-weighted blending.
- High-confidence evidence adjusts predictions more; low-confidence evidence
  has limited impact.
- The base values for status→start-probability and status→minutes-factor
  are derived from historical FPL data (correlation between announced
  availability and actual minutes), not hand-tuned constants.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fpl_intelligence.availability.providers import AvailabilityProvider
from fpl_intelligence.availability.state import state_to_adjustment
from fpl_intelligence.prediction.minutes import MinutesModel


class AvailabilityAwareMinutesModel:
    """Decorator that adjusts MinutesModel predictions using availability state.

    The base model predicts start probability and expected minutes from
    quantitative features. This wrapper blends those predictions with
    availability-derived factors using confidence-weighted interpolation:

        adjusted = base * (1 - c) + availability * c

    where ``c`` is the confidence of the availability evidence (0.0 to 1.0).
    When no evidence exists (confidence = 0.0), the wrapper passes through
    the base model output unchanged.
    """

    def __init__(self, base_model: MinutesModel, availability: AvailabilityProvider):
        self._base = base_model
        self._availability = availability

    @property
    def model_name(self) -> str:
        return f"{self._base.model_name}+availability"

    @property
    def model_version(self) -> str:
        return self._base.model_version

    def metadata(self) -> dict[str, Any]:
        base_meta = self._base.metadata()
        base_meta["model_name"] = self.model_name
        base_meta["availability_aware"] = True
        return base_meta

    def predict(self, X: Any, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return availability-adjusted predictions for each observation.

        ``context`` may contain ``player_id`` (int) and ``gameweek`` (int)
        to look up availability state. If absent, predictions pass through
        the base model unchanged.
        """
        ctx = context or {}
        player_id: int | None = ctx.get("player_id")
        gameweek: int | None = ctx.get("gameweek")

        base_preds = self._base.predict(X, context)

        if player_id is None or gameweek is None:
            return base_preds

        # Use the provider's availability lookup at the gameweek's deadline time.
        game_time = ctx.get("game_time")
        if game_time is None:
            # Fallback: use current time as a conservative proxy.
            from datetime import datetime

            game_time = datetime.now(UTC)

        status, confidence, _sources = self._availability.get_availability(player_id, game_time)
        adjustment = state_to_adjustment(status, confidence)

        adjusted: list[dict[str, Any]] = []
        for i, pred in enumerate(base_preds):
            if i == 0 and player_id is not None:
                # Only adjust the prediction for the requested player.
                pred = self._blend(pred, adjustment, confidence)
            adjusted.append(pred)

        return adjusted

    def _blend(
        self,
        pred: dict[str, Any],
        adjustment: dict[str, float],
        confidence: float,
    ) -> dict[str, Any]:
        """Confidence-weighted blend of base prediction and availability factor.

        When confidence is 0 (no evidence), returns pred unchanged.
        When confidence is 1 (fully certain), applies availability factor fully.
        """
        if confidence < 0.01:
            # No evidence — pass through with a provenance note.
            result = dict(pred)
            result["method"] = f"{result.get('method', 'unknown')}+availability(no_evidence)"
            return result

        base_start = float(pred.get("probability_starting", 0.0))
        base_minutes = float(pred.get("expected_minutes", 0.0))
        avail_start = adjustment["start_probability"]
        avail_minutes = adjustment["minutes_factor"] * 60.0  # baseline 60 min

        c = min(1.0, max(0.0, confidence))

        adjusted_start = base_start * (1.0 - c) + avail_start * c
        adjusted_minutes = base_minutes * (1.0 - c) + avail_minutes * c

        result = dict(pred)
        result["probability_starting"] = round(adjusted_start, 4)
        result["probability_60_plus"] = round(adjusted_start, 4)
        result["probability_30_plus"] = round(
            max(adjusted_start, pred.get("probability_30_plus", adjusted_start)), 4
        )
        result["expected_minutes"] = round(adjusted_minutes, 4)
        result["method"] = (
            f"{result.get('method', 'unknown')}+availability("
            f"status={self._status_label(result)}, conf={round(c, 4)})"
        )
        result["data_completeness"] = round(
            float(pred.get("data_completeness", 0.0)) * (1.0 - c) + c, 4
        )
        return result

    @staticmethod
    def _status_label(pred: dict[str, Any]) -> str:
        method = pred.get("method", "")
        if "no_evidence" in method:
            return "none"
        return "adjusted"
