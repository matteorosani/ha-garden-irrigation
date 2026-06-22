"""
Tests for bucket.py — water balance model.

Test strategy
-------------
We test every state transition independently, then simulate realistic
multi-day sequences to verify the system behaves correctly as a whole.

Key invariants we enforce throughout:
  - level is always in [0, max_capacity]
  - deficit_mm is always >= 0
  - percentage is always in [0, 100]
  - needs_water is True iff level < low_threshold
"""

from __future__ import annotations

import pytest
from garden_irrigation.bucket import BucketConfig, DailyResult, WaterBucket

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def config() -> BucketConfig:
    """Standard config: 25 mm max, 12 mm threshold."""
    return BucketConfig(max_capacity=25.0, low_threshold=12.0)


@pytest.fixture
def full_bucket(config) -> WaterBucket:
    """Bucket starting at full capacity."""
    return WaterBucket(config, initial_level=25.0)


@pytest.fixture
def empty_bucket(config) -> WaterBucket:
    """Bucket starting at 0."""
    return WaterBucket(config, initial_level=0.0)


@pytest.fixture
def half_bucket(config) -> WaterBucket:
    """Bucket starting at exactly the threshold (12 mm)."""
    return WaterBucket(config, initial_level=12.0)


# ── BucketConfig validation ────────────────────────────────────────────────────


class TestBucketConfig:
    def test_valid_config(self):
        cfg = BucketConfig(max_capacity=25.0, low_threshold=12.0)
        assert cfg.max_capacity == 25.0

    def test_zero_max_capacity_raises(self):
        with pytest.raises(ValueError, match="max_capacity"):
            BucketConfig(max_capacity=0.0, low_threshold=0.0)

    def test_negative_max_capacity_raises(self):
        with pytest.raises(ValueError):
            BucketConfig(max_capacity=-10.0, low_threshold=0.0)

    def test_threshold_above_max_raises(self):
        with pytest.raises(ValueError, match="low_threshold"):
            BucketConfig(max_capacity=25.0, low_threshold=30.0)

    def test_threshold_equal_to_max_is_valid(self):
        # Edge case: threshold == max means always water
        cfg = BucketConfig(max_capacity=25.0, low_threshold=25.0)
        assert cfg.low_threshold == 25.0

    def test_threshold_zero_is_valid(self):
        # threshold == 0 means never water automatically
        cfg = BucketConfig(max_capacity=25.0, low_threshold=0.0)
        assert cfg.low_threshold == 0.0

    def test_config_is_frozen(self):
        cfg = BucketConfig(max_capacity=25.0, low_threshold=12.0)
        with pytest.raises((AttributeError, TypeError)):
            cfg.max_capacity = 30.0  # type: ignore[misc]


# ── WaterBucket initialisation ─────────────────────────────────────────────────


class TestWaterBucketInit:
    def test_explicit_initial_level(self, config):
        b = WaterBucket(config, initial_level=15.0)
        assert b.level == pytest.approx(15.0)

    def test_default_initial_level_is_half(self, config):
        b = WaterBucket(config)
        assert b.level == pytest.approx(config.max_capacity / 2)

    def test_initial_level_above_max_is_clamped(self, config):
        b = WaterBucket(config, initial_level=99.0)
        assert b.level == pytest.approx(config.max_capacity)

    def test_initial_level_below_zero_is_clamped(self, config):
        b = WaterBucket(config, initial_level=-5.0)
        assert b.level == pytest.approx(0.0)


# ── Properties ─────────────────────────────────────────────────────────────────


class TestWaterBucketProperties:
    def test_percentage_full(self, full_bucket):
        assert full_bucket.percentage == pytest.approx(100.0)

    def test_percentage_empty(self, empty_bucket):
        assert empty_bucket.percentage == pytest.approx(0.0)

    def test_percentage_half(self, config):
        b = WaterBucket(config, initial_level=12.5)
        assert b.percentage == pytest.approx(50.0)

    def test_needs_water_above_threshold(self, full_bucket):
        assert full_bucket.needs_water is False

    def test_needs_water_below_threshold(self, empty_bucket):
        assert empty_bucket.needs_water is True

    def test_needs_water_exactly_at_threshold(self, half_bucket):
        # At exactly the threshold: does NOT need water (< not <=)
        assert half_bucket.needs_water is False

    def test_needs_water_one_below_threshold(self, config):
        b = WaterBucket(config, initial_level=11.99)
        assert b.needs_water is True

    def test_deficit_full_bucket(self, full_bucket):
        assert full_bucket.deficit_mm == pytest.approx(0.0)

    def test_deficit_empty_bucket(self, empty_bucket, config):
        assert empty_bucket.deficit_mm == pytest.approx(config.max_capacity)

    def test_deficit_partial_bucket(self, config):
        b = WaterBucket(config, initial_level=10.0)
        assert b.deficit_mm == pytest.approx(15.0)


# ── update() ──────────────────────────────────────────────────────────────────


class TestUpdate:
    def test_returns_daily_result(self, full_bucket):
        result = full_bucket.update(rain_mm=0.0, et0_mm=5.0, kc=1.0)
        assert isinstance(result, DailyResult)

    def test_pure_consumption_no_rain(self, full_bucket):
        # 5 mm ET₀, Kc=1.0, no rain → level drops by 5
        result = full_bucket.update(rain_mm=0.0, et0_mm=5.0, kc=1.0)
        assert result.level_after == pytest.approx(20.0)
        assert result.et_crop_mm == pytest.approx(5.0)
        assert result.rain_mm == pytest.approx(0.0)
        assert result.net_change == pytest.approx(-5.0)

    def test_rain_refills_bucket(self, empty_bucket):
        # 10 mm rain, no ET₀ → level rises to 10
        result = empty_bucket.update(rain_mm=10.0, et0_mm=0.0, kc=1.0)
        assert result.level_after == pytest.approx(10.0)

    def test_kc_scales_consumption(self, full_bucket):
        # ET₀=5, Kc=0.8 → consumption=4, level=21
        result = full_bucket.update(rain_mm=0.0, et0_mm=5.0, kc=0.8)
        assert result.et_crop_mm == pytest.approx(4.0)
        assert result.level_after == pytest.approx(21.0)

    def test_clamp_at_zero(self, empty_bucket):
        # Bucket can't go negative
        result = empty_bucket.update(rain_mm=0.0, et0_mm=10.0, kc=1.0)
        assert result.level_after == pytest.approx(0.0)
        assert result.was_clamped is True

    def test_clamp_at_max(self, full_bucket, config):
        # Heavy rain can't exceed max_capacity
        result = full_bucket.update(rain_mm=20.0, et0_mm=0.0, kc=1.0)
        assert result.level_after == pytest.approx(config.max_capacity)
        assert result.was_clamped is True

    def test_no_clamp_within_range(self, config):
        b = WaterBucket(config, initial_level=15.0)
        result = b.update(rain_mm=2.0, et0_mm=3.0, kc=1.0)
        # net = 2 - 3 = -1 → level = 14
        assert result.level_after == pytest.approx(14.0)
        assert result.was_clamped is False

    def test_level_before_and_after_tracked(self, config):
        b = WaterBucket(config, initial_level=20.0)
        result = b.update(rain_mm=0.0, et0_mm=4.0, kc=1.0)
        assert result.level_before == pytest.approx(20.0)
        assert result.level_after == pytest.approx(16.0)

    def test_negative_rain_treated_as_zero(self, full_bucket):
        # Bad data from weather provider: negative rain is ignored
        result = full_bucket.update(rain_mm=-5.0, et0_mm=3.0, kc=1.0)
        assert result.rain_mm == pytest.approx(0.0)
        assert result.level_after == pytest.approx(22.0)

    def test_negative_et0_treated_as_zero(self, half_bucket):
        result = half_bucket.update(rain_mm=0.0, et0_mm=-2.0, kc=1.0)
        assert result.et_crop_mm == pytest.approx(0.0)

    def test_level_updated_on_bucket(self, full_bucket):
        # Verify bucket.level reflects the update
        full_bucket.update(rain_mm=0.0, et0_mm=5.0, kc=1.0)
        assert full_bucket.level == pytest.approx(20.0)


# ── add_irrigation() ───────────────────────────────────────────────────────────


class TestAddIrrigation:
    def test_adds_water(self, empty_bucket):
        empty_bucket.add_irrigation(10.0)
        assert empty_bucket.level == pytest.approx(10.0)

    def test_does_not_exceed_max(self, full_bucket, config):
        full_bucket.add_irrigation(50.0)
        assert full_bucket.level == pytest.approx(config.max_capacity)

    def test_negative_irrigation_ignored(self, full_bucket):
        before = full_bucket.level
        full_bucket.add_irrigation(-5.0)
        assert full_bucket.level == pytest.approx(before)

    def test_zero_irrigation_no_change(self, half_bucket):
        before = half_bucket.level
        half_bucket.add_irrigation(0.0)
        assert half_bucket.level == pytest.approx(before)


# ── reset() ───────────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_from_empty(self, empty_bucket, config):
        empty_bucket.reset()
        assert empty_bucket.level == pytest.approx(config.max_capacity)

    def test_reset_from_partial(self, half_bucket, config):
        half_bucket.reset()
        assert half_bucket.level == pytest.approx(config.max_capacity)

    def test_reset_clears_needs_water(self, empty_bucket):
        assert empty_bucket.needs_water is True
        empty_bucket.reset()
        assert empty_bucket.needs_water is False


# ── Serialisation ──────────────────────────────────────────────────────────────


class TestSerialisation:
    def test_to_dict_contains_level(self, config):
        b = WaterBucket(config, initial_level=17.5)
        d = b.to_dict()
        assert "level" in d
        assert d["level"] == pytest.approx(17.5)

    def test_round_trip(self, config):
        original = WaterBucket(config, initial_level=18.3)
        restored = WaterBucket.from_dict(original.to_dict(), config)
        assert restored.level == pytest.approx(original.level)

    def test_from_dict_missing_level_uses_default(self, config):
        b = WaterBucket.from_dict({}, config)
        assert b.level == pytest.approx(config.max_capacity / 2)

    def test_from_dict_level_clamped_if_config_changed(self):
        # User had max=50, saved level=40, then changed max to 25
        old_config = BucketConfig(max_capacity=50.0, low_threshold=20.0)
        old_bucket = WaterBucket(old_config, initial_level=40.0)
        saved = old_bucket.to_dict()

        new_config = BucketConfig(max_capacity=25.0, low_threshold=12.0)
        restored = WaterBucket.from_dict(saved, new_config)
        # Level must be clamped to new max
        assert restored.level <= new_config.max_capacity


# ── Multi-day sequence (integration-style test) ────────────────────────────────


class TestMultiDaySequence:
    """
    Simulate a realistic week to verify the bucket behaves sensibly end-to-end.
    No mocking — just the math.
    """

    def test_dry_week_empties_bucket(self, config):
        b = WaterBucket(config, initial_level=25.0)
        # 7 days, hot (ET₀=6, Kc=1.0), no rain
        for _ in range(7):
            b.update(rain_mm=0.0, et0_mm=6.0, kc=1.0)
        # 25 - 7 * 6 = 25-42 → clamped at 0
        assert b.level == pytest.approx(0.0)
        assert b.needs_water is True

    def test_rainy_week_keeps_bucket_full(self, config):
        b = WaterBucket(config, initial_level=10.0)
        # 7 days with heavy rain (10 mm), low ET₀ (2 mm)
        for _ in range(7):
            b.update(rain_mm=10.0, et0_mm=2.0, kc=1.0)
        # Bucket should be at max
        assert b.level == pytest.approx(config.max_capacity)
        assert b.needs_water is False

    def test_irrigation_after_dry_spell_refills(self, config):
        b = WaterBucket(config, initial_level=25.0)
        # 5 dry days drain the bucket
        for _ in range(5):
            b.update(rain_mm=0.0, et0_mm=5.0, kc=1.0)
        assert b.needs_water is True
        # System irrigates: deliver deficit as mm
        b.add_irrigation(b.deficit_mm)
        assert b.level == pytest.approx(config.max_capacity)
        assert b.needs_water is False

    def test_level_never_leaves_valid_range(self, config):
        """Invariant: level always in [0, max_capacity] regardless of input."""
        b = WaterBucket(config, initial_level=12.0)
        scenarios = [
            (0.0, 10.0, 1.2),  # heavy consumption
            (50.0, 0.0, 0.5),  # torrential rain
            (0.0, 0.0, 1.0),  # nothing happens
            (5.0, 6.0, 0.8),  # net negative but small
        ]
        for rain, et0, kc in scenarios:
            b.update(rain_mm=rain, et0_mm=et0, kc=kc)
            assert 0.0 <= b.level <= config.max_capacity, (
                f"Level {b.level} out of range after update "
                f"(rain={rain}, et0={et0}, kc={kc})"
            )
