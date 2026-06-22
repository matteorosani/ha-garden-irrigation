"""
Tests for store.py — bucket state persistence.

Test strategy
-------------
``IrrigationStore`` is a thin coordinator around HA's ``Store``. We mock
``Store`` at the class level so no HA instance is needed.

The tests verify:
  1. Load returns a fresh bucket when no file exists.
  2. Load restores the correct zone from a multi-zone file.
  3. Load clamps the level when the saved value exceeds the current max.
  4. Save writes only the target zone without touching other zones.
  5. Remove deletes only the target zone.
  6. Remove deletes the file when the last zone is removed.
  7. Remove is a no-op when the zone doesn't exist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from garden_irrigation.bucket import BucketConfig, WaterBucket

# HA stubs are injected by tests/conftest.py before this file is collected.
from garden_irrigation.store import IrrigationStore

# ── Helpers ────────────────────────────────────────────────────────────────────

ENTRY_A = "entry_aaa111"
ENTRY_B = "entry_bbb222"

CFG = BucketConfig(max_capacity=25.0, low_threshold=12.0)


def _make_store(load_return: dict | None = None) -> tuple[IrrigationStore, MagicMock]:
    """
    Create an IrrigationStore whose inner Store is fully mocked.

    Returns (irrigation_store, mock_inner_store).
    The mock_inner_store has async_load / async_save / async_remove as AsyncMocks.
    """
    mock_inner = MagicMock()
    mock_inner.async_load = AsyncMock(return_value=load_return)
    mock_inner.async_save = AsyncMock()
    mock_inner.async_remove = AsyncMock()

    hass = MagicMock()
    store = IrrigationStore(hass)
    store._store = mock_inner  # replace the real Store with our mock
    return store, mock_inner


# ── async_load_bucket ─────────────────────────────────────────────────────────


class TestAsyncLoadBucket:
    @pytest.mark.asyncio
    async def test_fresh_bucket_when_no_file(self):
        store, _ = _make_store(load_return=None)
        bucket = await store.async_load_bucket(ENTRY_A, CFG)
        assert isinstance(bucket, WaterBucket)
        # Default initial level = 50 % of max
        assert bucket.level == pytest.approx(CFG.max_capacity / 2)

    @pytest.mark.asyncio
    async def test_fresh_bucket_when_entry_absent(self):
        # File exists but doesn't contain ENTRY_A
        store, _ = _make_store(load_return={ENTRY_B: {"level": 20.0}})
        bucket = await store.async_load_bucket(ENTRY_A, CFG)
        assert bucket.level == pytest.approx(CFG.max_capacity / 2)

    @pytest.mark.asyncio
    async def test_restores_saved_level(self):
        store, _ = _make_store(load_return={ENTRY_A: {"level": 17.3}})
        bucket = await store.async_load_bucket(ENTRY_A, CFG)
        assert bucket.level == pytest.approx(17.3)

    @pytest.mark.asyncio
    async def test_restores_correct_zone_from_multi_zone_file(self):
        data = {
            ENTRY_A: {"level": 10.0},
            ENTRY_B: {"level": 22.5},
        }
        store, _ = _make_store(load_return=data)

        bucket_a = await store.async_load_bucket(ENTRY_A, CFG)
        bucket_b = await store.async_load_bucket(ENTRY_B, CFG)

        assert bucket_a.level == pytest.approx(10.0)
        assert bucket_b.level == pytest.approx(22.5)

    @pytest.mark.asyncio
    async def test_level_clamped_when_config_max_reduced(self):
        # Saved level was 40 mm, but user changed max_capacity to 25 mm
        store, _ = _make_store(load_return={ENTRY_A: {"level": 40.0}})
        bucket = await store.async_load_bucket(ENTRY_A, CFG)  # max=25
        assert bucket.level <= CFG.max_capacity

    @pytest.mark.asyncio
    async def test_returns_water_bucket_instance(self):
        store, _ = _make_store(load_return=None)
        result = await store.async_load_bucket(ENTRY_A, CFG)
        assert isinstance(result, WaterBucket)


# ── async_save_bucket ─────────────────────────────────────────────────────────


class TestAsyncSaveBucket:
    @pytest.mark.asyncio
    async def test_saves_bucket_level(self):
        store, mock_inner = _make_store(load_return={})
        bucket = WaterBucket(CFG, initial_level=19.0)
        await store.async_save_bucket(ENTRY_A, bucket)

        mock_inner.async_save.assert_called_once()
        saved_data = mock_inner.async_save.call_args[0][0]
        assert ENTRY_A in saved_data
        assert saved_data[ENTRY_A]["level"] == pytest.approx(19.0)

    @pytest.mark.asyncio
    async def test_save_preserves_other_zones(self):
        # File already has ENTRY_B — saving ENTRY_A must not wipe it
        existing = {ENTRY_B: {"level": 22.0}}
        store, mock_inner = _make_store(load_return=existing)

        bucket = WaterBucket(CFG, initial_level=5.0)
        await store.async_save_bucket(ENTRY_A, bucket)

        saved_data = mock_inner.async_save.call_args[0][0]
        assert ENTRY_B in saved_data
        assert saved_data[ENTRY_B]["level"] == pytest.approx(22.0)

    @pytest.mark.asyncio
    async def test_save_overwrites_existing_entry(self):
        existing = {ENTRY_A: {"level": 10.0}}
        store, mock_inner = _make_store(load_return=existing)

        bucket = WaterBucket(CFG, initial_level=24.0)
        await store.async_save_bucket(ENTRY_A, bucket)

        saved_data = mock_inner.async_save.call_args[0][0]
        assert saved_data[ENTRY_A]["level"] == pytest.approx(24.0)

    @pytest.mark.asyncio
    async def test_save_creates_file_from_scratch(self):
        store, mock_inner = _make_store(load_return=None)
        bucket = WaterBucket(CFG, initial_level=15.0)
        await store.async_save_bucket(ENTRY_A, bucket)

        mock_inner.async_save.assert_called_once()
        saved_data = mock_inner.async_save.call_args[0][0]
        assert ENTRY_A in saved_data


# ── async_remove_zone ─────────────────────────────────────────────────────────


class TestAsyncRemoveZone:
    @pytest.mark.asyncio
    async def test_remove_deletes_only_target_zone(self):
        existing = {
            ENTRY_A: {"level": 10.0},
            ENTRY_B: {"level": 20.0},
        }
        store, mock_inner = _make_store(load_return=existing)
        await store.async_remove_zone(ENTRY_A)

        # async_save called with ENTRY_B still present, ENTRY_A gone
        mock_inner.async_save.assert_called_once()
        saved_data = mock_inner.async_save.call_args[0][0]
        assert ENTRY_A not in saved_data
        assert ENTRY_B in saved_data

    @pytest.mark.asyncio
    async def test_remove_last_zone_deletes_file(self):
        store, mock_inner = _make_store(load_return={ENTRY_A: {"level": 5.0}})
        await store.async_remove_zone(ENTRY_A)

        # File should be deleted, not written with an empty dict
        mock_inner.async_remove.assert_called_once()
        mock_inner.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_nonexistent_zone_is_noop(self):
        store, mock_inner = _make_store(load_return={ENTRY_B: {"level": 20.0}})
        # ENTRY_A doesn't exist in the file
        await store.async_remove_zone(ENTRY_A)

        # Nothing written — no save, no remove
        mock_inner.async_save.assert_not_called()
        mock_inner.async_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_from_empty_file_is_noop(self):
        store, mock_inner = _make_store(load_return=None)
        await store.async_remove_zone(ENTRY_A)
        mock_inner.async_save.assert_not_called()
        mock_inner.async_remove.assert_not_called()
