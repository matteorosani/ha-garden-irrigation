"""
Open-Meteo weather provider.

Fetches daily temperature and precipitation data from the Open-Meteo API.
Open-Meteo is free, requires no API key, and uses the ECMWF model — one of
the most accurate for European locations.

API reference: https://open-meteo.com/en/docs

Request strategy
----------------
We request past_days=1 and forecast_days=2, giving three daily rows:
  index 0 — yesterday  : actual settled precipitation  → bucket update
  index 1 — today      : forecast temperature          → ET₀ calculation
  index 2 — tomorrow   : forecast precipitation        → skip-irrigation decision

Running in the morning (07:00), yesterday's precipitation is the best
proxy for what actually soaked into the soil overnight. Today's temperature
forecast is used for ET₀ since min/max haven't been reached yet.
The skip decision combines today's remaining forecast and tomorrow's
forecast — if significant rain is coming in the next ~36 hours we wait.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import aiohttp

from . import WeatherData, WeatherProviderError

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://api.open-meteo.com/v1/forecast"

# Daily variables requested from the API
_DAILY_VARS = ",".join(
    [
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_sum",
        "wind_speed_10m_max",  # optional — for future Penman-Monteith
        "relative_humidity_2m_mean",  # optional — same
    ]
)


class OpenMeteoProvider:
    """
    Weather provider backed by the Open-Meteo forecast API.

    Parameters
    ----------
    latitude  : Location latitude in decimal degrees.
    longitude : Location longitude in decimal degrees.
    session   : An ``aiohttp.ClientSession`` to use for HTTP requests.
                In production, pass ``async_get_clientsession(hass)`` so
                Home Assistant manages the connection pool and cleanup.
                In tests, pass a mock.

    """

    def __init__(
        self,
        latitude: float,
        longitude: float,
        session: aiohttp.ClientSession,
    ) -> None:
        self._latitude = latitude
        self._longitude = longitude
        self._session = session

    async def get_data(self) -> WeatherData:
        """
        Fetch today's weather snapshot from Open-Meteo.

        Returns
        -------
        WeatherData  — normalised snapshot ready for the irrigator.

        Raises
        ------
        WeatherProviderError  — on any network error or unexpected response shape.

        """
        params: dict[str, Any] = {
            "latitude": self._latitude,
            "longitude": self._longitude,
            "daily": _DAILY_VARS,
            "past_days": 1,
            "forecast_days": 2,
            "timezone": "auto",  # dates returned in local timezone
        }

        _LOGGER.debug(
            "Fetching Open-Meteo data for (%.4f, %.4f)",
            self._latitude,
            self._longitude,
        )

        try:
            async with self._session.get(
                _BASE_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                raw = await resp.json()

        except aiohttp.ClientResponseError as err:
            raise WeatherProviderError(
                f"Open-Meteo returned HTTP {err.status}: {err.message}"
            ) from err
        except aiohttp.ClientError as err:
            raise WeatherProviderError(f"Open-Meteo request failed: {err}") from err
        except TimeoutError as err:
            raise WeatherProviderError(
                "Open-Meteo request timed out after 10 s"
            ) from err

        try:
            return self._parse(raw)
        except (KeyError, IndexError, TypeError, ValueError) as err:
            raise WeatherProviderError(
                f"Open-Meteo response has unexpected shape: {err}"
            ) from err

    # ── Parsing ────────────────────────────────────────────────────────────────

    def _parse(self, data: dict[str, Any]) -> WeatherData:
        """
        Extract a WeatherData from the raw Open-Meteo JSON response.

        With past_days=1 and forecast_days=2 the ``daily`` arrays have
        exactly 3 entries:
          [yesterday, today, tomorrow]

        This method is intentionally separated from ``get_data()`` so it
        can be tested synchronously without any HTTP machinery.
        """
        daily = data["daily"]

        # ── Temperatures (index 1 = today's forecast) ──────────────────────
        temp_max = _require_float(daily["temperature_2m_max"][1], "temperature_2m_max")
        temp_min = _require_float(daily["temperature_2m_min"][1], "temperature_2m_min")

        if temp_max < temp_min:
            _LOGGER.warning(
                "Open-Meteo: temp_max (%.1f) < temp_min (%.1f) — swapping",
                temp_max,
                temp_min,
            )
            temp_max, temp_min = temp_min, temp_max

        # ── Yesterday's actual precipitation (index 0) ─────────────────────
        precipitation_mm = _optional_float(daily["precipitation_sum"][0], default=0.0)

        # ── Forecast precipitation: today remaining + tomorrow (index 1+2) ──
        # Combining both gives a ~36-hour forward window, which is appropriate
        # when deciding whether to skip morning irrigation.
        forecast_today = _optional_float(daily["precipitation_sum"][1], default=0.0)
        forecast_tomorrow = _optional_float(daily["precipitation_sum"][2], default=0.0)
        forecast_precip_mm = (forecast_today if forecast_today is not None else 0.0) + (
            forecast_tomorrow if forecast_tomorrow is not None else 0.0
        )

        # ── Optional fields (logged but not required) ──────────────────────
        wind_speed_ms: float | None = None
        humidity_pct: float | None = None

        with contextlib.suppress(KeyError, IndexError):
            wind_speed_ms = _optional_float(daily["wind_speed_10m_max"][1])

        with contextlib.suppress(KeyError, IndexError):
            humidity_pct = _optional_float(daily["relative_humidity_2m_mean"][1])

        _LOGGER.debug(
            "Parsed weather: Tmin=%.1f Tmax=%.1f rain_yesterday=%.1f mm "
            "forecast_36h=%.1f mm",
            temp_min,
            temp_max,
            precipitation_mm,
            forecast_precip_mm,
        )

        return WeatherData(
            temp_min=temp_min,
            temp_max=temp_max,
            precipitation_mm=precipitation_mm if precipitation_mm is not None else 0.0,
            forecast_precip_mm=forecast_precip_mm,
            wind_speed_ms=wind_speed_ms,
            humidity_pct=humidity_pct,
        )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _require_float(value: Any, field: str) -> float:
    """Return ``float(value)`` or raise ``ValueError`` if None / unconvertible."""
    if value is None:
        raise ValueError(f"Required field '{field}' is null in API response")
    return float(value)


def _optional_float(value: Any, default: float | None = None) -> float | None:
    """Return ``float(value)`` if not None, otherwise ``default``."""
    if value is None:
        return default
    return float(value)
