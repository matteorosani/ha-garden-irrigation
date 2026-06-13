"""
Tests for irrigator.py — irrigation orchestrator.

Test strategy
-------------
The irrigator ties together et0, kc, and bucket. We don't re-test those
modules here — that's already done in test_et0, test_kc, test_bucket.
Instead we test the orchestration logic: the decisions, the conversions,
and the skip-reason routing.

We use a fixed date (2024-07-01, day 183) and a fixed planting date
(2024-04-01, 91 days earlier — tomato is in mid-season) so expected values
are stable and reproducible.
"""

from __future__ import annotations

from datetime import date

import pytest

from garden_irrigation.bucket import BucketConfig, WaterBucket
from garden_irrigation.irrigator import (
    SKIP_BUCKET_SUFFICIENT,
    SKIP_FORECAST_RAIN_SUFFICIENT,
    IrrigationResult,
    ZoneConfig,
    calculate,
)
from garden_irrigation.weather import WeatherData

# ── Shared constants ───────────────────────────────────────────────────────────

MILAN_LAT    = 45.47
TODAY        = date(2024, 7, 1)
PLANTING     = date(2024, 4, 1)   # 91 days before TODAY — tomato mid-season

# A zone with simple round numbers for easy manual verification
ZONE = ZoneConfig(
    crop_ids      = ["tomato"],
    planting_date = PLANTING,
    zone_area_m2  = 10.0,
    flow_rate_lpm = 5.0,
)

BUCKET_CFG = BucketConfig(max_capacity=25.0, low_threshold=12.0)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def dry_weather() -> WeatherData:
    """Hot, dry day — high ET₀, no rain, no forecast rain."""
    return WeatherData(
        temp_min=18.0, temp_max=32.0,
        precipitation_mm=0.0,
        forecast_precip_mm=0.0,
    )


@pytest.fixture
def rainy_weather() -> WeatherData:
    """Rainy day — low ET₀, significant rainfall."""
    return WeatherData(
        temp_min=14.0, temp_max=20.0,
        precipitation_mm=15.0,
        forecast_precip_mm=0.0,
    )


@pytest.fixture
def forecast_rain_weather() -> WeatherData:
    """Dry today but heavy rain forecast tomorrow."""
    return WeatherData(
        temp_min=18.0, temp_max=30.0,
        precipitation_mm=0.0,
        forecast_precip_mm=20.0,
    )


@pytest.fixture
def low_bucket(request) -> WaterBucket:
    """Bucket that definitely needs water (level = 5 mm, below 12 mm threshold)."""
    return WaterBucket(BUCKET_CFG, initial_level=5.0)


@pytest.fixture
def full_bucket() -> WaterBucket:
    return WaterBucket(BUCKET_CFG, initial_level=25.0)


# ── ZoneConfig validation ──────────────────────────────────────────────────────

class TestZoneConfig:

    def test_valid_config(self):
        z = ZoneConfig(["tomato"], date(2024, 4, 1), 10.0, 5.0)
        assert z.zone_area_m2 == 10.0

    def test_zero_area_raises(self):
        with pytest.raises(ValueError, match="zone_area_m2"):
            ZoneConfig(["tomato"], date(2024, 4, 1), 0.0, 5.0)

    def test_negative_area_raises(self):
        with pytest.raises(ValueError):
            ZoneConfig(["tomato"], date(2024, 4, 1), -5.0, 5.0)

    def test_zero_flow_rate_raises(self):
        with pytest.raises(ValueError, match="flow_rate_lpm"):
            ZoneConfig(["tomato"], date(2024, 4, 1), 10.0, 0.0)


# ── IrrigationResult shape ─────────────────────────────────────────────────────

class TestResultShape:

    def test_returns_irrigation_result(self, dry_weather, low_bucket):
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        assert isinstance(result, IrrigationResult)

    def test_all_fields_present(self, dry_weather, low_bucket):
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        assert result.should_water      is not None
        assert result.duration_minutes  is not None
        assert result.water_mm          is not None
        assert result.volume_liters     is not None
        assert result.et0_mm            is not None
        assert result.kc                is not None
        assert result.et_crop_mm        is not None
        assert result.daily             is not None
        assert result.bucket_level      is not None
        assert result.bucket_percentage is not None


# ── ET₀ and Kc passthrough ────────────────────────────────────────────────────

class TestEt0AndKc:

    def test_et0_is_positive_on_hot_day(self, dry_weather, low_bucket):
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        assert result.et0_mm > 0.0

    def test_et0_in_expected_range_for_milan_summer(self, dry_weather, low_bucket):
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        assert 4.0 < result.et0_mm < 9.0

    def test_kc_in_valid_range(self, dry_weather, low_bucket):
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        assert 0.3 < result.kc < 1.5

    def test_et_crop_equals_et0_times_kc(self, dry_weather, low_bucket):
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        assert result.et_crop_mm == pytest.approx(result.et0_mm * result.kc, rel=1e-3)

    def test_kc_near_mid_season_for_tomato_at_91_days(self, dry_weather, low_bucket):
        # Tomato at 91 days: l_ini=30, l_dev=40 → 91-70 = 21 days into l_mid
        # So Kc should be at or very near kc_mid = 1.15
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        assert result.kc == pytest.approx(1.15, abs=0.01)


# ── Should-water decision ──────────────────────────────────────────────────────

class TestWateringDecision:

    def test_waters_when_bucket_low_and_no_rain(self, dry_weather, low_bucket):
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        assert result.should_water is True
        assert result.skip_reason is None

    def test_skips_when_bucket_full(self, dry_weather, full_bucket):
        result = calculate(dry_weather, ZONE, full_bucket, TODAY, MILAN_LAT)
        assert result.should_water is False
        assert result.skip_reason == SKIP_BUCKET_SUFFICIENT

    def test_skips_when_bucket_sufficient_after_rain(self, rainy_weather):
        # Start half-full; heavy rain refills beyond threshold
        b = WaterBucket(BUCKET_CFG, initial_level=10.0)
        result = calculate(rainy_weather, ZONE, b, TODAY, MILAN_LAT)
        assert result.should_water is False
        assert result.skip_reason == SKIP_BUCKET_SUFFICIENT

    def test_skips_when_forecast_covers_deficit(self, forecast_rain_weather):
        # level=15: ET₀×Kc≈6.5 drains it to ~8.5 mm (below 12 threshold),
        # deficit becomes ~16.5 mm — covered by the 20 mm forecast.
        b = WaterBucket(BUCKET_CFG, initial_level=15.0)
        result = calculate(forecast_rain_weather, ZONE, b, TODAY, MILAN_LAT)
        assert result.should_water is False
        assert result.skip_reason == SKIP_FORECAST_RAIN_SUFFICIENT

    def test_partial_forecast_reduces_duration(self, low_bucket):
        # Partial forecast rain (5 mm) should reduce duration vs no forecast
        no_forecast = WeatherData(18.0, 32.0, precipitation_mm=0.0, forecast_precip_mm=0.0)
        some_forecast = WeatherData(18.0, 32.0, precipitation_mm=0.0, forecast_precip_mm=5.0)

        b1 = WaterBucket(BUCKET_CFG, initial_level=5.0)
        b2 = WaterBucket(BUCKET_CFG, initial_level=5.0)

        r1 = calculate(no_forecast,   ZONE, b1, TODAY, MILAN_LAT)
        r2 = calculate(some_forecast, ZONE, b2, TODAY, MILAN_LAT)

        assert r1.should_water is True
        assert r2.should_water is True
        assert r2.duration_minutes < r1.duration_minutes


# ── Volume and duration conversions ───────────────────────────────────────────

class TestConversions:

    def test_volume_equals_mm_times_area(self, dry_weather, low_bucket):
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        if result.should_water:
            assert result.volume_liters == pytest.approx(
                result.water_mm * ZONE.zone_area_m2, rel=1e-6
            )

    def test_duration_equals_volume_over_flow_rate(self, dry_weather, low_bucket):
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        if result.should_water:
            assert result.duration_minutes == pytest.approx(
                result.volume_liters / ZONE.flow_rate_lpm, rel=1e-3
            )

    def test_larger_area_means_more_volume_same_duration_ratio(self):
        # Doubling area doubles volume; duration also doubles (same flow rate)
        zone_small = ZoneConfig(["tomato"], PLANTING, zone_area_m2=10.0, flow_rate_lpm=5.0)
        zone_large = ZoneConfig(["tomato"], PLANTING, zone_area_m2=20.0, flow_rate_lpm=5.0)
        weather = WeatherData(18.0, 32.0, precipitation_mm=0.0, forecast_precip_mm=0.0)

        b1 = WaterBucket(BUCKET_CFG, initial_level=5.0)
        b2 = WaterBucket(BUCKET_CFG, initial_level=5.0)

        r1 = calculate(weather, zone_small, b1, TODAY, MILAN_LAT)
        r2 = calculate(weather, zone_large, b2, TODAY, MILAN_LAT)

        assert r2.volume_liters   == pytest.approx(r1.volume_liters   * 2, rel=1e-6)
        assert r2.duration_minutes == pytest.approx(r1.duration_minutes * 2, rel=1e-6)

    def test_higher_flow_rate_means_shorter_duration(self):
        zone_slow = ZoneConfig(["tomato"], PLANTING, zone_area_m2=10.0, flow_rate_lpm=2.0)
        zone_fast = ZoneConfig(["tomato"], PLANTING, zone_area_m2=10.0, flow_rate_lpm=8.0)
        weather = WeatherData(18.0, 32.0, precipitation_mm=0.0, forecast_precip_mm=0.0)

        b1 = WaterBucket(BUCKET_CFG, initial_level=5.0)
        b2 = WaterBucket(BUCKET_CFG, initial_level=5.0)

        r1 = calculate(weather, zone_slow, b1, TODAY, MILAN_LAT)
        r2 = calculate(weather, zone_fast, b2, TODAY, MILAN_LAT)

        assert r2.duration_minutes < r1.duration_minutes

    def test_no_water_fields_zero_when_skipping(self, dry_weather, full_bucket):
        result = calculate(dry_weather, ZONE, full_bucket, TODAY, MILAN_LAT)
        assert result.should_water     is False
        assert result.duration_minutes == 0.0
        assert result.water_mm         == 0.0
        assert result.volume_liters    == 0.0


# ── Bucket state after calculate() ────────────────────────────────────────────

class TestBucketMutation:

    def test_bucket_level_is_updated(self, dry_weather):
        b = WaterBucket(BUCKET_CFG, initial_level=25.0)
        before = b.level
        calculate(dry_weather, ZONE, b, TODAY, MILAN_LAT)
        # Hot dry day → bucket should drop
        assert b.level < before

    def test_result_bucket_level_matches_bucket(self, dry_weather, low_bucket):
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        assert result.bucket_level      == pytest.approx(low_bucket.level)
        assert result.bucket_percentage == pytest.approx(low_bucket.percentage)

    def test_bucket_not_refilled_by_calculate(self, dry_weather, low_bucket):
        # calculate() does NOT call add_irrigation — that's the coordinator's job
        result = calculate(dry_weather, ZONE, low_bucket, TODAY, MILAN_LAT)
        assert result.should_water is True
        # Bucket is still at the low level — caller must call add_irrigation()
        assert low_bucket.needs_water is True


# ── Seasonal Kc variation ──────────────────────────────────────────────────────

class TestSeasonalVariation:

    def test_kc_lower_at_seedling_than_mid_season(self):
        weather = WeatherData(18.0, 30.0, precipitation_mm=0.0, forecast_precip_mm=0.0)

        # Same planting date, compare day 5 (seedling) vs day 90 (mid-season)
        planting = date(2024, 4, 1)
        b1 = WaterBucket(BUCKET_CFG, initial_level=5.0)
        b2 = WaterBucket(BUCKET_CFG, initial_level=5.0)

        r_seedling   = calculate(weather, ZoneConfig(["tomato"], planting, 10.0, 5.0),
                                 b1, date(2024, 4, 6),  MILAN_LAT)   #  5 days
        r_midseason  = calculate(weather, ZoneConfig(["tomato"], planting, 10.0, 5.0),
                                 b2, date(2024, 7, 1),  MILAN_LAT)   # 91 days

        assert r_midseason.kc > r_seedling.kc