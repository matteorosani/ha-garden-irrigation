"""
Tests for store.py — bucket and pending watering persistence.

HA stubs are injected by tests/conftest.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from garden_irrigation.bucket import BucketConfig, WaterBucket
from garden_irrigation.store import IrrigationStore, PendingWatering

# -- Fixtures ------------------------------------------------------------------

ENTRY_A = "entry_aaa111"
ENTRY_B = "entry_bbb222"
CFG = BucketConfig(max_capacity=25.0, low_threshold=12.0)

SAMPLE_PENDING = PendingWatering(
    should_water=True,
    water_mm=12.5,
    duration_minutes=62.5,
    volume_liters=62.5,
)


def _make_store(load_return=None):
    """Create an IrrigationStore whose inner Store is fully mocked."""
    mock_inner = MagicMock()
    mock_inner.async_load = AsyncMock(return_value=load_return)
    mock_inner.async_save = AsyncMock()
    mock_inner.async_remove = AsyncMock()

    store = IrrigationStore(MagicMock())
    store._store = mock_inner
    return store, mock_inner


# -- PendingWatering -----------------------------------------------------------


class TestPendingWatering:
    def test_round_trip(self):
        d = SAMPLE_PENDING.to_dict()
        restored = PendingWatering.from_dict(d)
        assert restored.should_water is True
        assert restored.water_mm == pytest.approx(12.5)
        assert restored.duration_minutes == pytest.approx(62.5)
        assert restored.volume_liters == pytest.approx(62.5)

    def test_from_dict_coerces_types(self):
        # Values stored as JSON may come back as int
        p = PendingWatering.from_dict(
            {
                "should_water": 1,
                "water_mm": 10,
                "duration_minutes": 50,
                "volume_liters": 50,
            }
        )
        assert isinstance(p.should_water, bool)
        assert isinstance(p.water_mm, float)
        assert isinstance(p.duration_minutes, float)


# -- async_load_bucket ---------------------------------------------------------


class TestAsyncLoadBucket:
    @pytest.mark.asyncio
    async def test_fresh_bucket_when_no_file(self):
        store, _ = _make_store(load_return=None)
        bucket = await store.async_load_bucket(ENTRY_A, CFG)
        assert bucket.level == pytest.approx(CFG.max_capacity / 2)

    @pytest.mark.asyncio
    async def test_restores_saved_level(self):
        store, _ = _make_store({ENTRY_A: {"level": 17.3}})
        bucket = await store.async_load_bucket(ENTRY_A, CFG)
        assert bucket.level == pytest.approx(17.3)

    @pytest.mark.asyncio
    async def test_fresh_when_entry_absent(self):
        store, _ = _make_store({ENTRY_B: {"level": 20.0}})
        bucket = await store.async_load_bucket(ENTRY_A, CFG)
        assert bucket.level == pytest.approx(CFG.max_capacity / 2)

    @pytest.mark.asyncio
    async def test_level_clamped_when_max_reduced(self):
        store, _ = _make_store({ENTRY_A: {"level": 40.0}})
        bucket = await store.async_load_bucket(ENTRY_A, CFG)  # max=25
        assert bucket.level <= CFG.max_capacity

    @pytest.mark.asyncio
    async def test_restores_correct_zone(self):
        data = {ENTRY_A: {"level": 10.0}, ENTRY_B: {"level": 22.5}}
        store, _ = _make_store(data)
        b_a = await store.async_load_bucket(ENTRY_A, CFG)
        b_b = await store.async_load_bucket(ENTRY_B, CFG)
        assert b_a.level == pytest.approx(10.0)
        assert b_b.level == pytest.approx(22.5)


# -- async_load_pending --------------------------------------------------------


class TestAsyncLoadPending:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_file(self):
        store, _ = _make_store(None)
        assert await store.async_load_pending(ENTRY_A) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_pending_key(self):
        store, _ = _make_store({ENTRY_A: {"level": 10.0}})
        assert await store.async_load_pending(ENTRY_A) is None

    @pytest.mark.asyncio
    async def test_restores_pending(self):
        data = {ENTRY_A: {"level": 10.0, "pending": SAMPLE_PENDING.to_dict()}}
        store, _ = _make_store(data)
        pending = await store.async_load_pending(ENTRY_A)
        assert pending is not None
        assert pending.should_water is True
        assert pending.water_mm == pytest.approx(12.5)
        assert pending.duration_minutes == pytest.approx(62.5)

    @pytest.mark.asyncio
    async def test_returns_none_on_corrupt_pending(self):
        data = {ENTRY_A: {"level": 10.0, "pending": {"bad": "data"}}}
        store, _ = _make_store(data)
        assert await store.async_load_pending(ENTRY_A) is None


# -- async_save_bucket ---------------------------------------------------------


class TestAsyncSaveBucket:
    @pytest.mark.asyncio
    async def test_saves_level(self):
        store, mock = _make_store({})
        bucket = WaterBucket(CFG, initial_level=19.0)
        await store.async_save_bucket(ENTRY_A, bucket)
        saved = mock.async_save.call_args[0][0]
        assert saved[ENTRY_A]["level"] == pytest.approx(19.0)

    @pytest.mark.asyncio
    async def test_preserves_other_zones(self):
        store, mock = _make_store({ENTRY_B: {"level": 22.0}})
        bucket = WaterBucket(CFG, initial_level=5.0)
        await store.async_save_bucket(ENTRY_A, bucket)
        saved = mock.async_save.call_args[0][0]
        assert ENTRY_B in saved
        assert saved[ENTRY_B]["level"] == pytest.approx(22.0)

    @pytest.mark.asyncio
    async def test_does_not_clear_existing_pending(self):
        # async_save_bucket should NOT touch the pending key
        existing = {ENTRY_A: {"level": 10.0, "pending": SAMPLE_PENDING.to_dict()}}
        store, mock = _make_store(existing)
        bucket = WaterBucket(CFG, initial_level=8.0)
        await store.async_save_bucket(ENTRY_A, bucket)
        saved = mock.async_save.call_args[0][0]
        assert "pending" in saved[ENTRY_A]


# -- async_save_bucket_and_pending ---------------------------------------------


class TestAsyncSaveBucketAndPending:
    @pytest.mark.asyncio
    async def test_saves_pending(self):
        store, mock = _make_store({})
        bucket = WaterBucket(CFG, initial_level=8.0)
        await store.async_save_bucket_and_pending(ENTRY_A, bucket, SAMPLE_PENDING)
        saved = mock.async_save.call_args[0][0]
        assert saved[ENTRY_A]["pending"]["water_mm"] == pytest.approx(12.5)

    @pytest.mark.asyncio
    async def test_clears_pending_when_none(self):
        existing = {ENTRY_A: {"level": 8.0, "pending": SAMPLE_PENDING.to_dict()}}
        store, mock = _make_store(existing)
        bucket = WaterBucket(CFG, initial_level=22.0)
        await store.async_save_bucket_and_pending(ENTRY_A, bucket, None)
        saved = mock.async_save.call_args[0][0]
        assert "pending" not in saved[ENTRY_A]

    @pytest.mark.asyncio
    async def test_preserves_other_zones(self):
        existing = {ENTRY_B: {"level": 20.0}}
        store, mock = _make_store(existing)
        bucket = WaterBucket(CFG, initial_level=5.0)
        await store.async_save_bucket_and_pending(ENTRY_A, bucket, SAMPLE_PENDING)
        saved = mock.async_save.call_args[0][0]
        assert ENTRY_B in saved


# -- async_remove_zone ---------------------------------------------------------


class TestAsyncRemoveZone:
    @pytest.mark.asyncio
    async def test_removes_only_target(self):
        store, mock = _make_store({ENTRY_A: {"level": 10.0}, ENTRY_B: {"level": 20.0}})
        await store.async_remove_zone(ENTRY_A)
        saved = mock.async_save.call_args[0][0]
        assert ENTRY_A not in saved
        assert ENTRY_B in saved

    @pytest.mark.asyncio
    async def test_deletes_file_when_last_zone(self):
        store, mock = _make_store({ENTRY_A: {"level": 5.0}})
        await store.async_remove_zone(ENTRY_A)
        mock.async_remove.assert_called_once()
        mock.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_zone_absent(self):
        store, mock = _make_store({ENTRY_B: {"level": 20.0}})
        await store.async_remove_zone(ENTRY_A)
        mock.async_save.assert_not_called()
        mock.async_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_file_empty(self):
        store, mock = _make_store(None)
        await store.async_remove_zone(ENTRY_A)
        mock.async_save.assert_not_called()
        mock.async_remove.assert_not_called()
