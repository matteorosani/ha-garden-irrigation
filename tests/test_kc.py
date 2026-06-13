"""
Tests for kc.py — crop coefficient growth stage interpolation.

Test strategy
-------------
We test against a hand-crafted crop ("test_crop") whose simple numbers make
expected Kc values trivially calculable by inspection, plus a real crop
("tomato") loaded from crops.json to confirm the data pipeline works end-to-end.

Stage boundaries for test_crop
-------------------------------
  l_ini=10, l_dev=20, l_mid=30, l_late=10   → total 70 days
  kc_ini=0.5,  kc_mid=1.0,  kc_end=0.8

  Day  0-9   : Kc = 0.5           (initial, flat)
  Day 10-29  : Kc = 0.5 → 1.0    (development, linear)
  Day 30-59  : Kc = 1.0           (mid-season, flat)
  Day 60-69  : Kc = 1.0 → 0.8    (late, linear)
  Day 70+    : Kc = 0.8           (post-harvest)
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from garden_irrigation.kc import (
    CropDefinition,
    _lerp,
    available_crops,
    get_crop,
    kc_for_day,
    kc_for_zone,
    load_crops,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def simple_crop() -> CropDefinition:
    """A crop with round numbers so expected Kc is easy to compute by hand."""
    return CropDefinition(
        id="test_crop",
        name="Test Crop",
        kc_ini=0.5,
        kc_mid=1.0,
        kc_end=0.8,
        l_ini=10,
        l_dev=20,
        l_mid=30,
        l_late=10,
    )


@pytest.fixture
def crops_json(tmp_path) -> str:
    """Write a minimal crops.json to a temp file for loader tests."""
    data = {
        "crops": [
            {
                "id": "alpha",
                "name": "Alpha",
                "kc_ini": 0.6,
                "kc_mid": 1.1,
                "kc_end": 0.7,
                "l_ini": 10,
                "l_dev": 20,
                "l_mid": 30,
                "l_late": 10,
            },
            {
                "id": "beta",
                "name": "Beta",
                "kc_ini": 0.4,
                "kc_mid": 0.9,
                "kc_end": 0.6,
                "l_ini": 15,
                "l_dev": 25,
                "l_mid": 20,
                "l_late": 15,
            },
        ]
    }
    path = tmp_path / "crops.json"
    path.write_text(json.dumps(data))
    return str(path)


# ── _lerp ──────────────────────────────────────────────────────────────────────


class TestLerp:
    def test_start(self):
        assert _lerp(0.5, 1.0, 0.0) == pytest.approx(0.5)

    def test_end(self):
        assert _lerp(0.5, 1.0, 1.0) == pytest.approx(1.0)

    def test_midpoint(self):
        assert _lerp(0.5, 1.0, 0.5) == pytest.approx(0.75)

    def test_clamp_below_zero(self):
        assert _lerp(0.5, 1.0, -1.0) == pytest.approx(0.5)

    def test_clamp_above_one(self):
        assert _lerp(0.5, 1.0, 2.0) == pytest.approx(1.0)

    def test_descending(self):
        # Also works when b < a (late-season ramp down)
        assert _lerp(1.0, 0.8, 0.5) == pytest.approx(0.9)


# ── CropDefinition ─────────────────────────────────────────────────────────────


class TestCropDefinition:
    def test_total_days(self, simple_crop):
        assert simple_crop.total_days == 70  # 10+20+30+10

    def test_frozen(self, simple_crop):
        with pytest.raises((AttributeError, TypeError)):
            simple_crop.kc_ini = 0.9  # type: ignore[misc]


# ── kc_for_day ─────────────────────────────────────────────────────────────────


class TestKcForDay:
    # ── Initial stage (days 0-9) ──

    def test_initial_stage_day_0(self, simple_crop):
        assert kc_for_day(simple_crop, 0) == pytest.approx(0.5)

    def test_initial_stage_day_5(self, simple_crop):
        assert kc_for_day(simple_crop, 5) == pytest.approx(0.5)

    def test_initial_stage_last_day(self, simple_crop):
        assert kc_for_day(simple_crop, 9) == pytest.approx(0.5)

    def test_negative_days_treated_as_zero(self, simple_crop):
        assert kc_for_day(simple_crop, -5) == pytest.approx(0.5)

    # ── Development stage (days 10-29) ──

    def test_development_start(self, simple_crop):
        # Day 10: just entered dev stage, progress=0 → still kc_ini
        assert kc_for_day(simple_crop, 10) == pytest.approx(0.5)

    def test_development_midpoint(self, simple_crop):
        # Day 20: halfway through dev (progress=0.5) → 0.5 + 0.5*(1.0-0.5) = 0.75
        assert kc_for_day(simple_crop, 20) == pytest.approx(0.75)

    def test_development_almost_end(self, simple_crop):
        # Day 29: progress = (29-10)/20 = 0.95 → 0.5 + 0.95*0.5 = 0.975
        assert kc_for_day(simple_crop, 29) == pytest.approx(0.975)

    # ── Mid-season stage (days 30-59) ──

    def test_mid_season_start(self, simple_crop):
        assert kc_for_day(simple_crop, 30) == pytest.approx(1.0)

    def test_mid_season_middle(self, simple_crop):
        assert kc_for_day(simple_crop, 45) == pytest.approx(1.0)

    def test_mid_season_last_day(self, simple_crop):
        assert kc_for_day(simple_crop, 59) == pytest.approx(1.0)

    # ── Late-season stage (days 60-69) ──

    def test_late_season_start(self, simple_crop):
        # Day 60: progress=0 → kc_mid = 1.0
        assert kc_for_day(simple_crop, 60) == pytest.approx(1.0)

    def test_late_season_midpoint(self, simple_crop):
        # Day 65: progress=0.5 → lerp(1.0, 0.8, 0.5) = 0.9
        assert kc_for_day(simple_crop, 65) == pytest.approx(0.9)

    def test_late_season_last_day(self, simple_crop):
        # Day 69: progress=(69-60)/10=0.9 → lerp(1.0, 0.8, 0.9) = 0.82
        assert kc_for_day(simple_crop, 69) == pytest.approx(0.82)

    # ── Post-harvest ──

    def test_post_harvest_returns_kc_end(self, simple_crop):
        assert kc_for_day(simple_crop, 70) == pytest.approx(0.8)

    def test_far_past_harvest(self, simple_crop):
        assert kc_for_day(simple_crop, 500) == pytest.approx(0.8)

    # ── Continuity — no sudden jumps at boundaries ──

    def test_no_jump_at_ini_to_dev_boundary(self, simple_crop):
        kc_before = kc_for_day(simple_crop, 9)
        kc_after = kc_for_day(simple_crop, 10)
        assert abs(kc_after - kc_before) < 0.01

    def test_no_jump_at_dev_to_mid_boundary(self, simple_crop):
        kc_before = kc_for_day(simple_crop, 29)
        kc_after = kc_for_day(simple_crop, 30)
        assert abs(kc_after - kc_before) < 0.05

    def test_no_jump_at_mid_to_late_boundary(self, simple_crop):
        kc_before = kc_for_day(simple_crop, 59)
        kc_after = kc_for_day(simple_crop, 60)
        assert abs(kc_after - kc_before) < 0.01


# ── load_crops / get_crop / available_crops ────────────────────────────────────


class TestCropLoader:
    def test_load_from_custom_path(self, crops_json):
        registry = load_crops(crops_json)
        assert "alpha" in registry
        assert "beta" in registry

    def test_crop_fields_loaded_correctly(self, crops_json):
        crop = load_crops(crops_json)["alpha"]
        assert crop.name == "Alpha"
        assert crop.kc_mid == pytest.approx(1.1)
        assert crop.l_dev == 20

    def test_get_crop_real_file(self):
        # Reads the real crops.json bundled with the integration
        tomato = get_crop("tomato")
        assert tomato.name == "Tomato"
        assert tomato.kc_mid == pytest.approx(1.15)

    def test_get_crop_unknown_raises(self):
        with pytest.raises(KeyError):
            get_crop("does_not_exist")

    def test_available_crops_sorted(self):
        crops = available_crops()
        names = [c.name for c in crops]
        assert names == sorted(names)

    def test_available_crops_nonempty(self):
        assert len(available_crops()) > 0


# ── kc_for_zone ────────────────────────────────────────────────────────────────


class TestKcForZone:
    def test_single_crop_matches_kc_for_day(self):
        # Single crop in zone should equal kc_for_day directly
        planting = date(2024, 4, 1)
        today = date(2024, 7, 1)  # 91 days later — mid-season for tomato
        tomato = get_crop("tomato")

        result_zone = kc_for_zone(["tomato"], planting, today)
        result_direct = kc_for_day(tomato, (today - planting).days)
        assert result_zone == pytest.approx(result_direct)

    def test_multi_crop_is_average(self):
        # Two crops: average of their individual Kc values on the same day
        planting = date(2024, 4, 1)
        today = date(2024, 7, 15)  # 105 days
        registry = load_crops()

        kc_tomato = kc_for_day(registry["tomato"], (today - planting).days)
        kc_pumpkin = kc_for_day(registry["pumpkin"], (today - planting).days)
        expected = (kc_tomato + kc_pumpkin) / 2

        result = kc_for_zone(["tomato", "pumpkin"], planting, today)
        assert result == pytest.approx(expected)

    def test_three_crops_average(self):
        planting = date(2024, 4, 15)
        today = date(2024, 7, 4)  # 80 days
        registry = load_crops()
        days = (today - planting).days

        expected = (
            kc_for_day(registry["tomato"], days)
            + kc_for_day(registry["pumpkin"], days)
            + kc_for_day(registry["bean_green"], days)
        ) / 3

        result = kc_for_zone(["tomato", "pumpkin", "bean_green"], planting, today)
        assert result == pytest.approx(expected)

    def test_empty_crop_list_returns_safe_default(self):
        # No crops configured → don't under-water; return 1.0
        result = kc_for_zone([], date(2024, 4, 1), date(2024, 7, 1))
        assert result == pytest.approx(1.0)

    def test_unknown_crop_ids_ignored(self):
        # Unknown IDs are silently skipped; known ones still averaged
        planting = date(2024, 4, 1)
        today = date(2024, 7, 1)
        result_clean = kc_for_zone(["tomato"], planting, today)
        result_with_junk = kc_for_zone(["tomato", "nonexistent_crop"], planting, today)
        assert result_clean == pytest.approx(result_with_junk)

    def test_planting_day_uses_kc_ini(self):
        # On the planting day itself, Kc should be kc_ini for all crops
        today = date(2024, 5, 1)
        result = kc_for_zone(["tomato"], planting_date=today, today=today)
        tomato = get_crop("tomato")
        assert result == pytest.approx(tomato.kc_ini)

    def test_kc_rises_during_season(self):
        # Kc in July should be higher than Kc in April for the same planting
        planting = date(2024, 4, 1)
        kc_early = kc_for_zone(["tomato"], planting, date(2024, 5, 1))
        kc_peak = kc_for_zone(["tomato"], planting, date(2024, 7, 1))
        assert kc_peak > kc_early
