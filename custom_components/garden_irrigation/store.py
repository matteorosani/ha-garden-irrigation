"""
Bucket state persistence.

Wraps Home Assistant's ``helpers.storage.Store`` to save and restore the
water bucket level for every zone across HA restarts.

All zones share a single JSON file (``garden_irrigation`` in ``.storage/``),
keyed by config entry ID. This avoids cluttering ``.storage/`` with one file
per zone and makes it easy to migrate or back up all zone state at once.

File layout
-----------
.storage/garden_irrigation
{
    "version": 1,
    "data": {
        "<entry_id_zone_1>": {
            "level": 18.5,
            "last_result": {
                "should_water": true,
                "duration_minutes": 24.0,
                "water_mm": 8.5,
                "volume_liters": 102.0,
                "et0_mm": 6.2,
                "kc": 1.15,
                "et_crop_mm": 7.13,
                "skip_reason": null
            }
        }
    }
}

Persisting last_result means the watering duration sensor survives HA restarts
and the morning automation can always read a valid value even if it starts before
the next evening calculation.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .bucket import BucketConfig, WaterBucket
from .const import STORAGE_KEY, STORAGE_VERSION
from .irrigator import IrrigationResult

_LOGGER = logging.getLogger(__name__)


class IrrigationStore:
    """
    Persist and restore bucket state for all irrigation zones.

    One instance is shared across all zones in the integration
    (created once in ``async_setup`` and passed to each zone's coordinator).

    Parameters
    ----------
    hass : The HomeAssistant instance.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def async_load_zone(
        self,
        entry_id: str,
        config: BucketConfig,
    ) -> tuple[WaterBucket, IrrigationResult | None]:
        """
        Load persisted bucket and last_result for a zone.

        Returns a fresh bucket at 50% capacity and None for last_result
        when no persisted state exists (first run after installation).

        Persisting last_result means the watering duration sensor always
        shows the previous night's recommendation, even after HA restarts,
        so the morning automation can read a valid value hours later.
        """
        data = await self._load_all()
        zone_data = data.get(entry_id, {})

        bucket = (
            WaterBucket.from_dict(zone_data, config)
            if zone_data
            else WaterBucket(config)
        )

        last_result: IrrigationResult | None = None
        if "last_result" in zone_data:
            try:
                last_result = IrrigationResult(**zone_data["last_result"])
            except (TypeError, KeyError):
                _LOGGER.debug(
                    "Could not restore last_result for %s — will be None until "
                    "next calculation",
                    entry_id,
                )

        _LOGGER.debug(
            "Restored zone %s: bucket=%.1f mm  last_result=%s",
            entry_id,
            bucket.level,
            "present" if last_result else "absent",
        )
        return bucket, last_result

    # Keep the old name as an alias for backward compatibility with tests
    async def async_load_bucket(
        self,
        entry_id: str,
        config: BucketConfig,
    ) -> WaterBucket:
        """Load only the bucket (legacy — prefer async_load_zone)."""
        bucket, _ = await self.async_load_zone(entry_id, config)
        return bucket

    async def async_save_zone(
        self,
        entry_id: str,
        bucket: WaterBucket,
        last_result: IrrigationResult | None = None,
    ) -> None:
        """
        Persist bucket level and optionally last_result for a zone.

        Both are stored in the same JSON entry so a single file write
        captures the full zone state atomically.
        """
        data = await self._load_all()
        zone_data = bucket.to_dict()

        if last_result is not None:
            # IrrigationResult contains a nested DailyResult — serialise manually
            zone_data["last_result"] = {
                "should_water": last_result.should_water,
                "duration_minutes": last_result.duration_minutes,
                "water_mm": last_result.water_mm,
                "volume_liters": last_result.volume_liters,
                "et0_mm": last_result.et0_mm,
                "kc": last_result.kc,
                "et_crop_mm": last_result.et_crop_mm,
                "bucket_level": last_result.bucket_level,
                "bucket_percentage": last_result.bucket_percentage,
                "skip_reason": last_result.skip_reason,
                "daily": {
                    "et_crop_mm": last_result.daily.et_crop_mm,
                    "rain_mm": last_result.daily.rain_mm,
                    "net_change": last_result.daily.net_change,
                    "level_before": last_result.daily.level_before,
                    "level_after": last_result.daily.level_after,
                    "was_clamped": last_result.daily.was_clamped,
                },
            }

        data[entry_id] = zone_data
        await self._store.async_save(data)

    async def async_remove_zone(self, entry_id: str) -> None:
        """
        Remove a zone's persisted state.

        Called from ``async_unload_entry`` when the user deletes a zone.
        If this was the last zone, the storage file is deleted entirely.

        Parameters
        ----------
        entry_id : HA config entry ID for the zone being removed.
        """
        data = await self._load_all()

        if entry_id not in data:
            return  # nothing to remove

        del data[entry_id]
        _LOGGER.debug("Removed persisted state for entry %s", entry_id)

        if data:
            # Other zones still have state — write the trimmed file
            await self._store.async_save(data)
        else:
            # No zones left — delete the file entirely
            await self._store.async_remove()
            _LOGGER.debug("All zone state removed; deleted storage file")

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _load_all(self) -> dict[str, Any]:
        """
        Load the full storage dict.

        Returns an empty dict (not None) when the file doesn't exist yet,
        so callers never need to guard against None.
        """
        data = await self._store.async_load()
        return data if isinstance(data, dict) else {}
