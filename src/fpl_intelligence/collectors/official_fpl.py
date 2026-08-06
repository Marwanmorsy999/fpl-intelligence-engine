from collections.abc import Mapping
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class OfficialFPLDataProvider:
    """Adapter for the public FPL web application's JSON endpoints."""

    def __init__(self, base_url: str, timeout: float = 20.0, max_retries: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _get_json(self, path: str) -> Any:
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            response = client.get(f"{self.base_url}{path}")
            response.raise_for_status()
            return response.json()

    def get_bootstrap_static(self) -> Mapping[str, object]:
        payload = self._get_json("/api/bootstrap-static/")
        if not isinstance(payload, dict):
            raise TypeError("bootstrap-static response must be a JSON object")
        return payload

    def get_fixtures(self) -> list[Mapping[str, object]]:
        payload = self._get_json("/api/fixtures/")
        if not isinstance(payload, list):
            raise TypeError("fixtures response must be a JSON array")
        return payload

    def get_player_summary(self, player_id: int) -> Mapping[str, object]:
        payload = self._get_json(f"/api/element-summary/{player_id}/")
        if not isinstance(payload, dict):
            raise TypeError("player summary response must be a JSON object")
        return payload
