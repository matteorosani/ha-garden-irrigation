"""
Irrigation orchestrator.

Ties together ET₀ calculation, crop coefficient interpolation, and the water
bucket model to answer one daily question per zone:

    "Should we water today, and if so, for how many minutes?"

This module contains no I/O and no Home Assistant dependencies. It receives
a ``WeatherData`` snapshot and returns an ``IrrigationResult``. The HA layer
(coordinator in ``__init__.py``) is responsible for:
  - fetching the weather data before calling ``calculate()``
  - opening the valve for ``result.duration_minutes`` after a True result
  - calling ``bucket.add_irrigation(result.water_mm)`` once watering completes

Keeping computation and execution separate makes the logic fully unit-testable
without any HA infrastructure.

Skip reasons
------------
When ``should_water`` is False, ``skip_reason`` explains why:
  "bucket_sufficient"       — level is above the low threshold, no deficit
  "forecast_rain_sufficient"— forecast rain will cover the full deficit
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .bucket import DailyResult, WaterBucket
from .et0 import et0_for_date
from .kc import kc_for_zone
from .weather import WeatherData

# Human-readable skip reason constants (also used in sensor attributes)
SKIP_BUCKET_SUFFICIENT = "bucket_sufficient"
SKIP_FORECAST_RAIN_SUFFICIENT = "forecast_rain_sufficient"


# ── Zone configuration ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ZoneConfig:
    """
    Static configuration for one irrigation zone.

    Stored in the HA config entry; passed into ``calculate()`` on every run.

    Attributes
    ----------
    crop_ids       : List of crop ID strings matching entries in crops.json.
    planting_date  : Date the crops were planted. Used to compute growth stage.
    zone_area_m2   : Area covered by this zone's drip system  [m²].
                     Used to convert mm → litres  (1 mm * 1 m² = 1 litre).
    flow_rate_lpm  : Total water output of this zone's drip system  [L/min].
                     Used to convert litres → minutes.

    """

    crop_ids: list[str]
    planting_date: date
    zone_area_m2: float
    flow_rate_lpm: float

    def __post_init__(self) -> None:
        if self.zone_area_m2 <= 0:
            raise ValueError(f"zone_area_m2 must be > 0, got {self.zone_area_m2}")
        if self.flow_rate_lpm <= 0:
            raise ValueError(f"flow_rate_lpm must be > 0, got {self.flow_rate_lpm}")


# ── Result ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IrrigationResult:
    """
    Output of a single ``calculate()`` call.

    All sensor entities in ``sensor.py`` are driven by fields from this object.
    Nothing is recomputed — this is the single source of truth for the day.

    Attributes
    ----------
    should_water      : True if the valve should open today.
    duration_minutes  : How long to run the valve  [min]. 0 when should_water=False.
    water_mm          : Irrigation depth to deliver  [mm]. 0 when should_water=False.
    volume_liters     : Total water volume  [L] = water_mm * zone_area_m2.
    et0_mm            : Reference ET₀ computed for today  [mm/day].
    kc                : Effective crop coefficient for today (average of all crops).
    et_crop_mm        : Actual crop water demand = ET₀ * Kc  [mm/day].
    daily             : Full DailyResult from the bucket update (rain, net change, etc.)
    bucket_level      : Bucket level after today's update  [mm].
    bucket_percentage : Bucket level as % of max capacity.
    skip_reason       : Human-readable reason for not watering, or None.

    """

    should_water: bool
    duration_minutes: float
    water_mm: float
    volume_liters: float
    et0_mm: float
    kc: float
    et_crop_mm: float
    daily: DailyResult
    bucket_level: float
    bucket_percentage: float
    skip_reason: str | None


# ── Main function ──────────────────────────────────────────────────────────────


def calculate(
    weather: WeatherData,
    zone: ZoneConfig,
    bucket: WaterBucket,
    today: date,
    latitude_deg: float,
) -> IrrigationResult:
    """
    Run the full daily irrigation calculation for one zone.

    Steps
    -----
    1. Compute ET₀ from today's temperatures and latitude.
    2. Compute the effective Kc for today's growth stage.
    3. Update the bucket: level += rain - (ET₀ * Kc), clamped to [0, max].
    4. If the bucket is still above threshold → skip, bucket is sufficient.
    5. Compute how much water is needed to reach max_capacity.
    6. Subtract forecast rain: if rain is coming, reduce (or eliminate) watering.
    7. Convert mm → litres → minutes and return the result.

    Parameters
    ----------
    weather      : Today's weather snapshot (temperatures + rain).
    zone         : Static zone configuration (crops, area, flow rate).
    bucket       : The zone's water balance tracker (mutated in place at step 3).
    today        : The date for which to calculate.
    latitude_deg : Location latitude in decimal degrees (from hass.config).

    Returns
    -------
    IrrigationResult — immutable snapshot; sensor entities read from this.

    Note
    ----
    This function mutates ``bucket`` (step 3). After the valve runs, the caller
    must also call ``bucket.add_irrigation(result.water_mm)`` to register the
    water delivered. That call belongs in the HA coordinator, not here.

    """
    # ── 1. ET₀ ────────────────────────────────────────────────────────────────
    et0 = et0_for_date(
        t_min=weather.temp_min,
        t_max=weather.temp_max,
        day=today,
        latitude_deg=latitude_deg,
    )

    # ── 2. Crop coefficient ───────────────────────────────────────────────────
    kc = kc_for_zone(
        crop_ids=zone.crop_ids,
        planting_date=zone.planting_date,
        today=today,
    )

    et_crop = round(et0 * kc, 3)

    # ── 3. Bucket update ──────────────────────────────────────────────────────
    daily = bucket.update(
        rain_mm=weather.precipitation_mm,
        et0_mm=et0,
        kc=kc,
    )

    # ── 4. Check if watering is needed ────────────────────────────────────────
    if not bucket.needs_water:
        return IrrigationResult(
            should_water=False,
            duration_minutes=0.0,
            water_mm=0.0,
            volume_liters=0.0,
            et0_mm=round(et0, 3),
            kc=round(kc, 3),
            et_crop_mm=et_crop,
            daily=daily,
            bucket_level=bucket.level,
            bucket_percentage=bucket.percentage,
            skip_reason=SKIP_BUCKET_SUFFICIENT,
        )

    # ── 5. Compute water needed ───────────────────────────────────────────────
    # Target: refill bucket to max_capacity.
    deficit_mm = bucket.deficit_mm

    # ── 6. Account for forecast rain ─────────────────────────────────────────
    # If significant rain is coming, reduce the irrigation amount.
    # We never schedule negative watering — floor at 0.
    forecast_mm = max(0.0, weather.forecast_precip_mm)
    water_mm = max(0.0, deficit_mm - forecast_mm)

    if water_mm == 0.0:
        return IrrigationResult(
            should_water=False,
            duration_minutes=0.0,
            water_mm=0.0,
            volume_liters=0.0,
            et0_mm=round(et0, 3),
            kc=round(kc, 3),
            et_crop_mm=et_crop,
            daily=daily,
            bucket_level=bucket.level,
            bucket_percentage=bucket.percentage,
            skip_reason=SKIP_FORECAST_RAIN_SUFFICIENT,
        )

    # ── 7. Convert to volume and duration ─────────────────────────────────────
    # 1 mm of water over 1 m² = exactly 1 litre.
    volume_liters = water_mm * zone.zone_area_m2
    duration_minutes = volume_liters / zone.flow_rate_lpm

    return IrrigationResult(
        should_water=True,
        duration_minutes=round(duration_minutes, 1),
        water_mm=round(water_mm, 2),
        volume_liters=round(volume_liters, 1),
        et0_mm=round(et0, 3),
        kc=round(kc, 3),
        et_crop_mm=et_crop,
        daily=daily,
        bucket_level=bucket.level,
        bucket_percentage=bucket.percentage,
        skip_reason=None,
    )
