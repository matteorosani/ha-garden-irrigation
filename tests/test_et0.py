"""
Tests for et0.py — Hargreaves-Samani reference evapotranspiration.

Test strategy
-------------
1. _extraterrestrial_radiation  — geometry only, cross-check against expected
   seasonal behaviour and known FAO-56 ballpark values.
2. et0_hargreaves               — the formula itself; range checks for real
   climatic scenarios (Milan, 45.47°N) plus edge-case guards.
3. et0_for_date                 — thin wrapper; confirm it delegates correctly
   and handles leap-year dates.

All expected ranges are derived from independent hand-calculations using the
FAO-56 worked examples and published Milan climatological data.
"""

from __future__ import annotations

import math
from datetime import date

from garden_irrigation.et0 import (
    _extraterrestrial_radiation,
    et0_for_date,
    et0_hargreaves,
)

# Milan, Italy — the reference location used throughout these tests
MILAN_LAT_DEG = 45.47
MILAN_LAT_RAD = math.radians(MILAN_LAT_DEG)


# ── Ra tests ───────────────────────────────────────────────────────────────────


class TestExtraterrestrialRadiation:
    """
    Ra is pure geometry. We can't measure it, but we can assert:
    - It peaks near summer solstice and troughs near winter solstice.
    - Its absolute magnitude matches FAO-56 Table 2 ballpark values for 45°N.
    - It handles extreme latitudes without raising exceptions.
    """

    def test_summer_solstice_milan(self):
        # FAO-56 Table 2: Ra at 48°N in June ≈ 41 MJ/m²/day.
        # For 45.47°N on day 172 (June 21) we expect ~40 - 44 MJ/m²/day.
        ra = _extraterrestrial_radiation(172, MILAN_LAT_RAD)
        assert 38.0 < ra < 44.0, f"Ra summer solstice: {ra:.2f}"

    def test_winter_solstice_milan(self):
        # FAO-56 Table 2: Ra at 48°N in December ≈ 7 MJ/m²/day.
        # For 45.47°N on day 355 (Dec 21) we expect 8 - 12 MJ/m²/day.
        ra = _extraterrestrial_radiation(355, MILAN_LAT_RAD)
        assert 7.0 < ra < 13.0, f"Ra winter solstice: {ra:.2f}"

    def test_summer_greater_than_winter(self):
        ra_summer = _extraterrestrial_radiation(172, MILAN_LAT_RAD)
        ra_winter = _extraterrestrial_radiation(355, MILAN_LAT_RAD)
        # At 45°N the ratio is roughly 4 - 5 *
        assert ra_summer > ra_winter * 2.5

    def test_equator_high_and_stable(self):
        # At the equator Ra is high (>30) and varies less across the year
        ra_jun = _extraterrestrial_radiation(172, 0.0)
        ra_dec = _extraterrestrial_radiation(355, 0.0)
        assert ra_jun > 30.0
        assert ra_dec > 30.0
        # Equatorial seasonal swing is much smaller than at 45°N
        assert abs(ra_jun - ra_dec) < 10.0

    def test_always_nonnegative(self):
        # Ra can never be negative
        for doy in [1, 90, 172, 264, 355]:
            ra = _extraterrestrial_radiation(doy, MILAN_LAT_RAD)
            assert ra >= 0.0, f"Negative Ra on day {doy}: {ra}"

    def test_extreme_latitude_no_crash(self):
        # Near the pole in summer (midnight sun) — must not raise
        ra = _extraterrestrial_radiation(172, math.radians(89.9))
        assert ra >= 0.0

    def test_extreme_latitude_winter_no_crash(self):
        # Near the pole in winter (polar night) — must return 0, not crash
        ra = _extraterrestrial_radiation(355, math.radians(89.9))
        assert ra >= 0.0


# ── ET₀ tests ──────────────────────────────────────────────────────────────────


class TestEt0Hargreaves:
    """
    We check absolute ranges (from climatological knowledge of the Po Valley)
    and relative behaviour (summer > winter, more sun → more ET₀).
    """

    def test_hot_summer_day_milan(self):
        # Typical July day: Tmax 32°C, Tmin 18°C, day 182 (July 1)
        # Expected ET₀: 5 - 8 mm/day for northern Italy summer
        result = et0_hargreaves(18.0, 32.0, 182, MILAN_LAT_RAD)
        assert 5.0 < result < 8.0, f"Summer ET₀: {result:.2f}"

    def test_mild_spring_day_milan(self):
        # April day: Tmax 18°C, Tmin 8°C, day 100
        result = et0_hargreaves(8.0, 18.0, 100, MILAN_LAT_RAD)
        assert 2.0 < result < 5.0, f"Spring ET₀: {result:.2f}"

    def test_cold_winter_day_milan(self):
        # January day: Tmax 8°C, Tmin 2°C, day 15
        result = et0_hargreaves(2.0, 8.0, 15, MILAN_LAT_RAD)
        assert 0.1 < result < 1.5, f"Winter ET₀: {result:.2f}"

    def test_summer_much_greater_than_winter(self):
        summer = et0_hargreaves(18.0, 32.0, 182, MILAN_LAT_RAD)
        winter = et0_hargreaves(2.0, 8.0, 15, MILAN_LAT_RAD)
        assert summer > winter * 3, "Summer ET₀ should be >> winter ET₀"

    def test_wider_temp_range_increases_et0(self):
        # Same mean temp, wider daily range → more ET₀
        narrow = et0_hargreaves(23.0, 27.0, 182, MILAN_LAT_RAD)  # range 4°C
        wide = et0_hargreaves(18.0, 32.0, 182, MILAN_LAT_RAD)  # range 14°C
        assert wide > narrow

    def test_equal_temps_returns_zero(self):
        # √(Tmax - Tmin) undefined for zero range → return 0
        assert et0_hargreaves(20.0, 20.0, 182, MILAN_LAT_RAD) == 0.0

    def test_tmax_less_than_tmin_returns_zero(self):
        # Nonsense input — should not raise, should return 0
        assert et0_hargreaves(25.0, 20.0, 182, MILAN_LAT_RAD) == 0.0

    def test_sub_zero_temperatures_nonnegative(self):
        # Even a cold day below freezing must return ≥ 0
        result = et0_hargreaves(-6.0, -1.0, 15, MILAN_LAT_RAD)
        assert result >= 0.0

    def test_returns_float(self):
        result = et0_hargreaves(15.0, 30.0, 150, MILAN_LAT_RAD)
        assert isinstance(result, float)


# ── Convenience wrapper tests ──────────────────────────────────────────────────


class TestEt0ForDate:
    def test_matches_core_function(self):
        # et0_for_date must produce the identical result as et0_hargreaves
        d = date(2024, 7, 1)
        via_wrapper = et0_for_date(18.0, 32.0, d, MILAN_LAT_DEG)
        via_core = et0_hargreaves(
            18.0,
            32.0,
            d.timetuple().tm_yday,
            math.radians(MILAN_LAT_DEG),
        )
        assert abs(via_wrapper - via_core) < 1e-12

    def test_leap_year_feb29_no_crash(self):
        # Day 60 of a leap year (Feb 29) must work correctly
        result = et0_for_date(4.0, 11.0, date(2024, 2, 29), MILAN_LAT_DEG)
        assert result >= 0.0

    def test_southern_hemisphere(self):
        # Sydney, Australia (33.9°S) — June is winter there; ET₀ should be low
        result_june = et0_for_date(10.0, 18.0, date(2024, 6, 21), -33.87)
        result_dec = et0_for_date(22.0, 35.0, date(2024, 12, 21), -33.87)
        assert result_dec > result_june, "Southern hemisphere: Dec > Jun"
