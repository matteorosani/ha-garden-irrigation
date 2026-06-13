"""
Weather abstraction layer.

Defines the data contract (``WeatherData``) and the provider protocol
(``WeatherProvider``) that all concrete implementations must satisfy.

Adding a new weather source means writing a class with one async method —
``get_data()`` — and returning a ``WeatherData`` instance. The rest of the
integration never needs to change.

Current implementations
-----------------------
- ``open_meteo.py`` — free, no API key, uses the ECMWF model (Step 6)

Planned
-------
- Local weather station (reads from HA sensor entities directly)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class WeatherData:
    """
    Normalised daily weather snapshot consumed by the irrigator.

    All concrete weather providers must return data in this shape.

    Attributes
    ----------
    temp_min           : Daily minimum temperature  [°C]
    temp_max           : Daily maximum temperature  [°C]
    precipitation_mm   : Measured rainfall for today  [mm]
                         Used to update the water bucket.
    forecast_precip_mm : Forecast rainfall for the next 24 h  [mm]
                         Used to decide whether to skip irrigation
                         (no point watering if heavy rain is coming).
    wind_speed_ms      : Mean wind speed  [m/s]  — optional, reserved for
                         future Penman-Monteith upgrade.
    humidity_pct       : Relative humidity  [%]  — optional, same.

    """

    temp_min: float
    temp_max: float
    precipitation_mm: float
    forecast_precip_mm: float
    wind_speed_ms: float | None = field(default=None)
    humidity_pct: float | None = field(default=None)


@runtime_checkable
class WeatherProvider(Protocol):
    """
    Protocol (interface) that every weather source must implement.

    Using ``Protocol`` rather than an abstract base class means concrete
    providers don't need to inherit from anything — they just need to have
    the right method. This makes them easier to test with simple stubs.
    """

    async def get_data(self) -> WeatherData:
        """
        Fetch or compute today's weather data.

        Must be async because real providers make HTTP requests.
        Raises ``WeatherProviderError`` on failure.
        """
        ...


class WeatherProviderError(Exception):
    """Raised when a weather provider cannot return valid data."""
