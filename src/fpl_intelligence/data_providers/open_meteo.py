"""Phase 15.0 — Open-Meteo weather connector (free, NO API key).

Open-Meteo publishes free weather forecasts without any key
(https://open-meteo.com, non-commercial terms). This connector:

* hardcodes lat/lon for all 20 Premier League stadiums;
* fetches the matchday forecast (precipitation, wind) for upcoming fixtures
  with a 6 h cache TTL;
* normalises conditions into a display string plus a *small, documented*
  adjustment signal that is ONLY non-zero for severe weather.

Severity policy (documented, deterministic):

* ``rain_mm >= 8`` OR ``wind_kph >= 40``  -> "severe"      (adjustment -0.3)
* ``rain_mm >= 3`` OR ``wind_kph >= 25``  -> "noticeable"  (display only)
* otherwise                                -> "clear"

Severe weather yields a small negative xPTS adjustment for the affected
team's players (passing games suffer; set-piece threat rises). The reason
string is always shown in the UI when an adjustment is applied.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

#: FPL team id -> stadium coordinates. FPL team IDs are stable across seasons;
#: entries marked (spare) cover legacy ids that no current club uses.
PL_STADIUM_COORDS: dict[int, dict[str, float]] = {
    1: {"lat": 51.5549, "lon": -0.1084, "name": "Emirates Stadium"},          # Arsenal
    2: {"lat": 52.5398, "lon": -1.8847, "name": "Villa Park"},                # Aston Villa
    3: {"lat": 50.7352, "lon": -1.8383, "name": "Vitality Stadium"},          # Bournemouth
    4: {"lat": 51.4419, "lon": -0.2946, "name": "Gtech Community Stadium"},   # Brentford
    5: {"lat": 50.8644, "lon": -0.0902, "name": "Amex Stadium"},              # Brighton
    6: {"lat": 51.4817, "lon": -3.1791, "name": "Cardiff City Stadium"},      # (spare)
    7: {"lat": 51.4751, "lon": -0.2216, "name": "Stamford Bridge"},           # Chelsea
    8: {"lat": 51.5326, "lon": -0.1665, "name": "Selhurst Park"},             # Crystal Palace
    9: {"lat": 53.4397, "lon": -2.1619, "name": "Hill Dickinson Stadium"},    # Everton
    10: {"lat": 51.5519, "lon": -0.0687, "name": "Craven Cottage"},           # Fulham
    11: {"lat": 52.0553, "lon": 1.4204, "name": "Portman Road"},              # (spare)
    12: {"lat": 53.7299, "lon": -1.8689, "name": "Elland Road"},              # Leeds
    13: {"lat": 51.5959, "lon": 0.0450, "name": "London Stadium"},            # (spare)
    14: {"lat": 53.4308, "lon": -2.9608, "name": "Anfield"},                  # Liverpool
    15: {"lat": 53.5030, "lon": -2.2020, "name": "City of Manchester Stadium"},
    16: {"lat": 54.9145, "lon": -1.6219, "name": "St James' Park"},           # Newcastle
    17: {"lat": 52.6323, "lon": -1.6650, "name": "Turf Moor"},                # (spare)
    18: {"lat": 52.6203, "lon": -1.1424, "name": "King Power Stadium"},       # (spare)
    19: {"lat": 52.5092, "lon": -1.8882, "name": "St Andrew's"},              # (spare)
    20: {"lat": 52.5869, "lon": -2.1290, "name": "Molineux Stadium"},         # Wolves
}


@dataclass
class WeatherOutlook:
    """Normalised matchday weather for one stadium/date."""

    lat: float
    lon: float
    stadium: str
    forecast_date: str
    precipitation_mm: float
    wind_kph: float
    severity: str  # "clear" | "noticeable" | "severe"
    adjustment: float  # documented xPTS delta (only non-zero when severe)
    reason: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "stadium": self.stadium,
            "date": self.forecast_date,
            "precipitation_mm": round(self.precipitation_mm, 1),
            "wind_kph": round(self.wind_kph, 1),
            "severity": self.severity,
            "adjustment": self.adjustment,
            "reason": self.reason,
        }


def classify_weather(precipitation_mm: float, wind_kph: float) -> tuple[str, float, str]:
    """Return (severity, adjustment, reason) from raw forecast values."""
    if precipitation_mm >= 8.0 or wind_kph >= 40.0:
        return (
            "severe",
            -0.3,
            "Rain + high wind forecast: set-piece threat up, passing game down",
        )
    if precipitation_mm >= 3.0 or wind_kph >= 25.0:
        return ("noticeable", 0.0, "Some rain/wind forecast — minor impact")
    return ("clear", 0.0, "No significant weather impact forecast")


def parse_forecast_payload(
    payload: dict[str, Any],
    *,
    lat: float,
    lon: float,
    stadium: str,
    target_date: str | None = None,
) -> WeatherOutlook:
    """Normalise an Open-Meteo daily forecast response for one stadium.

    Picks the first forecast day, or the entry matching ``target_date``
    (``YYYY-MM-DD``) when provided.
    """
    daily = (payload.get("daily") or {}) if isinstance(payload, dict) else {}
    times = daily.get("time") or []
    rain = daily.get("precipitation_sum") or daily.get("rain_sum") or []
    wind = daily.get("wind_speed_10m_max") or daily.get("windspeed_10m_max") or []
    if not times:
        raise ValueError("Open-Meteo payload has no daily forecast rows")

    idx = 0
    if target_date:
        for i, stamp in enumerate(times):
            if str(stamp).startswith(target_date):
                idx = i
                break

    def _f(seq: list[Any], i: int) -> float:
        try:
            return float(seq[i])
        except (IndexError, TypeError, ValueError):
            return 0.0

    precipitation_mm = max(0.0, _f(rain, idx))
    wind_raw = _f(wind, idx)
    # Open-Meteo default wind unit is km/h; be defensive about m/s payloads.
    units = payload.get("daily_units") or {}
    wind_kph = wind_raw * 3.6 if units.get("wind_speed_10m_max") in ("m/s", "ms") else wind_raw
    severity, adjustment, reason = classify_weather(precipitation_mm, wind_kph)
    return WeatherOutlook(
        lat=lat,
        lon=lon,
        stadium=stadium,
        forecast_date=str(times[idx])[:10],
        precipitation_mm=precipitation_mm,
        wind_kph=wind_kph,
        severity=severity,
        adjustment=adjustment,
        reason=reason,
    )


class OpenMeteoConnector:
    """Cache-first Open-Meteo connector (no key, no auth, polite defaults)."""

    name = "open_meteo"

    def __init__(
        self,
        *,
        base_url: str = OPEN_METEO_FORECAST_URL,
        cache: Any = None,
        http_client: httpx.Client | None = None,
        timeout: float = 15.0,
        ttl_seconds: int = 6 * 3600,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._cache = cache
        self._client = http_client or httpx.Client(timeout=timeout)
        self._owns_client = http_client is None
        self._ttl = ttl_seconds

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def stadium_coords(self, fpl_team_id: int) -> dict[str, float] | None:
        info = PL_STADIUM_COORDS.get(fpl_team_id)
        if not info:
            return None
        return {"lat": info["lat"], "lon": info["lon"], "name": info["name"]}

    def fetch_matchday_outlook(
        self,
        fpl_team_id: int,
        *,
        target_date: str | None = None,
        forecast_days: int = 3,
    ) -> WeatherOutlook | None:
        """Fetch the weather outlook for a team's stadium; None when unknown.

        Any network/parse failure returns ``None`` — weather is display-only
        enrichment and must never break a decisions request.
        """
        stadium = self.stadium_coords(fpl_team_id)
        if stadium is None:
            return None
        cache_key = f"{self.name}:{fpl_team_id}:{target_date or 'next'}:{forecast_days}"
        cached = self._cache.get(cache_key) if self._cache is not None else None
        if isinstance(cached, dict):
            try:
                return parse_forecast_payload(
                    cached,
                    lat=stadium["lat"],
                    lon=stadium["lon"],
                    stadium=stadium["name"],
                    target_date=target_date,
                )
            except ValueError:
                return None
        params = {
            "latitude": stadium["lat"],
            "longitude": stadium["lon"],
            "daily": "precipitation_sum,wind_speed_10m_max",
            "timezone": "Europe/London",
            "forecast_days": forecast_days,
        }
        try:
            response = self._client.get(self._base_url, params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001 - graceful degradation is the contract
            return None
        if self._cache is not None and hasattr(self._cache, "set"):
            self._cache.set(cache_key, payload, self._ttl)
        try:
            return parse_forecast_payload(
                payload,
                lat=stadium["lat"],
                lon=stadium["lon"],
                stadium=stadium["name"],
                target_date=target_date,
            )
        except ValueError:
            return None
