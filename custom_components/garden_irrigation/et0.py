"""
Reference Evapotranspiration — Hargreaves-Samani method (1985).

The Hargreaves-Samani formula estimates ET₀ from daily min/max temperature
and extraterrestrial radiation Ra, which is computed purely from latitude and
day-of-year. No humidity or wind sensor is required.

Formula
-------
    ET₀  =  0.0023 x Ra_mm x (Tmean + 17.8) x sqrt(Tmax - Tmin)

where Ra_mm is extraterrestrial radiation expressed in mm/day of water-equivalent
evaporation (Ra in MJ/m^2/day divided by the latent heat of vaporisation, 2.45 MJ/kg).

References
----------
Hargreaves, G.H. & Samani, Z.A. (1985). Reference crop evapotranspiration
    from temperature. Applied Engineering in Agriculture, 1(2), 96-99.

Allen, R.G. et al. (1998). Crop evapotranspiration — Guidelines for computing
    crop water requirements. FAO Irrigation and Drainage Paper 56. FAO, Rome.
    Equations 21-27 (Ra), Section 3.2.1 (Hargreaves).

"""

from __future__ import annotations

import math
from datetime import date

# ── Physical constants ─────────────────────────────────────────────────────────
_GSC = 0.0820  # Solar constant  [MJ/m^2/min)]
_LATENT_HEAT = 2.45  # Latent heat of vaporisation of water  [MJ/kg]


# ── Internal helpers ───────────────────────────────────────────────────────────


def _extraterrestrial_radiation(day_of_year: int, latitude_rad: float) -> float:
    """
    Extraterrestrial radiation Ra  [MJ/m^2/day].

    This is the solar radiation that would reach a horizontal surface at the
    top of the atmosphere on a given day at a given latitude — no clouds,
    no atmosphere. It depends only on geometry.

    Parameters
    ----------
    day_of_year :  1-365  (or 366 for leap years)
    latitude_rad:  latitude in radians; negative values = southern hemisphere

    Returns
    -------
    Ra in MJ/m^2/day  (always >= 0)

    """
    # Inverse relative distance Earth-Sun  (FAO-56 eq. 23)
    # Accounts for the elliptical orbit: Earth is ~3 % closer in January.
    dr = 1.0 + 0.033 * math.cos(2.0 * math.pi / 365.0 * day_of_year)

    # Solar declination  [rad]  (FAO-56 eq. 24)
    # The tilt of Earth's axis relative to the Sun; +23.45° at June solstice.
    decl = 0.409 * math.sin(2.0 * math.pi / 365.0 * day_of_year - 1.39)

    # Sunset hour angle  [rad]  (FAO-56 eq. 25)
    # How far past noon the Sun sets; larger = longer days.
    # The argument is clamped to [-1, 1] to guard against domain errors at
    # extreme latitudes where the Sun never rises/sets.
    arg = -math.tan(latitude_rad) * math.tan(decl)
    arg = max(-1.0, min(1.0, arg))
    ws = math.acos(arg)

    # Ra  (FAO-56 eq. 21)
    ra = (
        (24.0 * 60.0 / math.pi)
        * _GSC
        * dr
        * (
            ws * math.sin(latitude_rad) * math.sin(decl)
            + math.cos(latitude_rad) * math.cos(decl) * math.sin(ws)
        )
    )
    return max(0.0, ra)


# ── Public API ─────────────────────────────────────────────────────────────────


def et0_hargreaves(
    t_min: float,
    t_max: float,
    day_of_year: int,
    latitude_rad: float,
) -> float:
    """
    Reference evapotranspiration ET₀  [mm/day] via Hargreaves-Samani.

    Parameters
    ----------
    t_min :        Daily minimum temperature  [°C]
    t_max :        Daily maximum temperature  [°C]
    day_of_year :  Day of year (1-365/366)
    latitude_rad:  Latitude in radians

    Returns
    -------
    ET₀ in mm/day (always >= 0).
    Returns 0.0 if Tmax <= Tmin (invalid range — can't take sqrt of non-positive).

    """
    if t_max <= t_min:
        return 0.0

    t_mean = (t_min + t_max) / 2.0

    ra_mj = _extraterrestrial_radiation(day_of_year, latitude_rad)
    # Convert from energy units to water depth:
    # 1 MJ/m^2 can evaporate (1 / 2.45) kg/m^2 = 0.408 mm of water.
    ra_mm = ra_mj / _LATENT_HEAT

    et0 = 0.0023 * ra_mm * (t_mean + 17.8) * math.sqrt(t_max - t_min)
    return max(0.0, et0)


def et0_for_date(
    t_min: float,
    t_max: float,
    day: date,
    latitude_deg: float,
) -> float:
    """
    Convenience wrapper that accepts a ``date`` object and latitude in degrees.

    This is the function the rest of the integration will call day-to-day.

    Parameters
    ----------
    t_min :        Daily minimum temperature  [°C]
    t_max :        Daily maximum temperature  [°C]
    day :          The date to calculate for
    latitude_deg:  Latitude in decimal degrees (e.g. 45.47 for Milan)

    Returns
    -------
    ET₀ in mm/day (always >= 0)

    """
    return et0_hargreaves(
        t_min=t_min,
        t_max=t_max,
        day_of_year=day.timetuple().tm_yday,
        latitude_rad=math.radians(latitude_deg),
    )
