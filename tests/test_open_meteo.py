"""
Tests for weather/open_meteo.py — Open-Meteo weather provider.

Test strategy
-------------
We split the two responsibilities of OpenMeteoProvider:

1. ``_parse()`` — synchronous, pure data transformation.
   Tested directly with realistic sample JSON dicts. No mocking needed.
   This covers: correct index selection, None handling, temp swap, field
   extraction, optional fields present/absent.

2. ``get_data()`` — async, does HTTP.
   Tested with a mock aiohttp session. We verify that:
   - A successful response calls _parse() and returns WeatherData.
   - HTTP errors are wrapped in WeatherProviderError.
   - Network errors are wrapped in WeatherProviderError.
   - Malformed JSON (unexpected shape) is wrapped in WeatherProviderError.

We never hit the real Open-Meteo API in tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from garden_irrigation.weather import WeatherData, WeatherProviderError
from garden_irrigation.weather.open_meteo import (
    OpenMeteoProvider,
    _optional_float,
    _require_float,
)

# ── Sample API response ────────────────────────────────────────────────────────


def _sample_response(
    tmax=(28.0, 32.0, 30.0),
    tmin=(15.0, 18.0, 17.0),
    precip=(5.2, 0.0, 3.0),
    wind=(None, 4.5, None),
    humidity=(None, 65.0, None),
) -> dict:
    """
    Build a realistic Open-Meteo daily response dict.

    Indices: [yesterday, today, tomorrow]
    Default: yesterday had 5.2 mm rain; today hot (32/18); tomorrow 3 mm expected.
    """
    return {
        "latitude": 45.47,
        "longitude": 9.18,
        "timezone": "Europe/Rome",
        "daily": {
            "time": ["2024-06-30", "2024-07-01", "2024-07-02"],
            "temperature_2m_max": list(tmax),
            "temperature_2m_min": list(tmin),
            "precipitation_sum": list(precip),
            "wind_speed_10m_max": list(wind),
            "relative_humidity_2m_mean": list(humidity),
        },
    }


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def provider() -> OpenMeteoProvider:
    """Provider with a dummy session (only used for _parse tests)."""
    return OpenMeteoProvider(
        latitude=45.47,
        longitude=9.18,
        session=MagicMock(spec=aiohttp.ClientSession),
    )


# ── _require_float / _optional_float ──────────────────────────────────────────


class TestHelpers:
    def test_require_float_converts(self):
        assert _require_float(3, "x") == pytest.approx(3.0)

    def test_require_float_none_raises(self):
        with pytest.raises(ValueError, match="myfield"):
            _require_float(None, "myfield")

    def test_optional_float_converts(self):
        assert _optional_float(5.5) == pytest.approx(5.5)

    def test_optional_float_none_returns_default(self):
        assert _optional_float(None, default=0.0) == pytest.approx(0.0)

    def test_optional_float_none_no_default_returns_none(self):
        assert _optional_float(None) is None


# ── _parse() ──────────────────────────────────────────────────────────────────


class TestParse:
    def test_returns_weather_data(self, provider):
        result = provider._parse(_sample_response())
        assert isinstance(result, WeatherData)

    def test_temperature_taken_from_today_index(self, provider):
        # Index 1 = today
        result = provider._parse(_sample_response())
        assert result.temp_max == pytest.approx(32.0)
        assert result.temp_min == pytest.approx(18.0)

    def test_precipitation_taken_from_yesterday(self, provider):
        # Index 0 = yesterday's actual rain
        result = provider._parse(_sample_response())
        assert result.precipitation_mm == pytest.approx(5.2)

    def test_forecast_precip_is_today_plus_tomorrow(self, provider):
        # today=0.0, tomorrow=3.0 → total=3.0
        result = provider._parse(_sample_response())
        assert result.forecast_precip_mm == pytest.approx(3.0)

    def test_forecast_precip_sums_both_days(self, provider):
        result = provider._parse(_sample_response(precip=(2.0, 4.0, 6.0)))
        assert result.forecast_precip_mm == pytest.approx(10.0)  # 4+6

    def test_null_yesterday_rain_defaults_to_zero(self, provider):
        result = provider._parse(_sample_response(precip=(None, 0.0, 2.0)))
        assert result.precipitation_mm == pytest.approx(0.0)

    def test_null_forecast_rain_defaults_to_zero(self, provider):
        result = provider._parse(_sample_response(precip=(1.0, None, None)))
        assert result.forecast_precip_mm == pytest.approx(0.0)

    def test_swaps_inverted_temperatures(self, provider):
        # API occasionally returns max < min — we correct it gracefully
        result = provider._parse(
            _sample_response(tmax=(28.0, 18.0, 26.0), tmin=(15.0, 32.0, 14.0))
        )
        assert result.temp_max >= result.temp_min

    def test_optional_wind_present(self, provider):
        result = provider._parse(_sample_response())
        assert result.wind_speed_ms == pytest.approx(4.5)

    def test_optional_wind_absent(self, provider):
        # wind column missing entirely from response
        data = _sample_response()
        del data["daily"]["wind_speed_10m_max"]
        result = provider._parse(data)
        assert result.wind_speed_ms is None

    def test_optional_wind_null_today(self, provider):
        result = provider._parse(_sample_response(wind=(3.0, None, 2.0)))
        assert result.wind_speed_ms is None

    def test_optional_humidity_present(self, provider):
        result = provider._parse(_sample_response())
        assert result.humidity_pct == pytest.approx(65.0)

    def test_optional_humidity_absent(self, provider):
        data = _sample_response()
        del data["daily"]["relative_humidity_2m_mean"]
        result = provider._parse(data)
        assert result.humidity_pct is None

    def test_missing_required_field_raises(self, provider):
        data = _sample_response()
        del data["daily"]["temperature_2m_max"]
        with pytest.raises((WeatherProviderError, KeyError)):
            provider._parse(data)

    def test_none_required_temp_raises(self, provider):
        # temperature_2m_max[1] = None → required field missing
        result_data = _sample_response(tmax=(28.0, None, 30.0))
        with pytest.raises((WeatherProviderError, ValueError)):
            provider._parse(result_data)


# ── get_data() — async, uses mock session ─────────────────────────────────────


class TestGetData:
    def _make_mock_session(self, json_data: dict) -> MagicMock:
        """Build a mock aiohttp session that returns json_data on GET."""
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=json_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock(spec=aiohttp.ClientSession)
        mock_session.get = MagicMock(return_value=mock_resp)
        return mock_session

    def _make_error_session(self, exc: Exception) -> MagicMock:
        """Build a mock session whose GET raises exc."""
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(side_effect=exc)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock(spec=aiohttp.ClientSession)
        mock_session.get = MagicMock(return_value=mock_resp)
        return mock_session

    @pytest.mark.asyncio
    async def test_successful_fetch_returns_weather_data(self):
        session = self._make_mock_session(_sample_response())
        provider = OpenMeteoProvider(45.47, 9.18, session)
        result = await provider.get_data()
        assert isinstance(result, WeatherData)
        assert result.temp_max == pytest.approx(32.0)

    @pytest.mark.asyncio
    async def test_correct_url_called(self):
        session = self._make_mock_session(_sample_response())
        provider = OpenMeteoProvider(45.47, 9.18, session)
        await provider.get_data()
        session.get.assert_called_once()
        call_args = session.get.call_args
        assert "open-meteo.com" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_latitude_longitude_in_params(self):
        session = self._make_mock_session(_sample_response())
        provider = OpenMeteoProvider(45.47, 9.18, session)
        await provider.get_data()
        params = session.get.call_args[1]["params"]
        assert params["latitude"] == 45.47
        assert params["longitude"] == 9.18

    @pytest.mark.asyncio
    async def test_http_error_raises_provider_error(self):
        exc = aiohttp.ClientResponseError(
            request_info=MagicMock(), history=(), status=429
        )
        session = self._make_error_session(exc)
        provider = OpenMeteoProvider(45.47, 9.18, session)
        with pytest.raises(WeatherProviderError, match="429"):
            await provider.get_data()

    @pytest.mark.asyncio
    async def test_network_error_raises_provider_error(self):
        session = self._make_error_session(
            aiohttp.ClientConnectorError(
                connection_key=MagicMock(), os_error=OSError("refused")
            )
        )
        provider = OpenMeteoProvider(45.47, 9.18, session)
        with pytest.raises(WeatherProviderError):
            await provider.get_data()

    @pytest.mark.asyncio
    async def test_malformed_response_raises_provider_error(self):
        session = self._make_mock_session({"unexpected": "shape"})
        provider = OpenMeteoProvider(45.47, 9.18, session)
        with pytest.raises(WeatherProviderError, match="unexpected shape"):
            await provider.get_data()
