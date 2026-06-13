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
        "<entry_id_zone_1>": {"level": 18.5},
        "<entry_id_zone_2>": {"level": 22.1}
    }
}

Usage pattern (in __init__.py)
-------------------------------
    store  = IrrigationStore(hass)
    bucket = await store.async_load_bucket(entry.entry_id, bucket_config)
    # ... daily calculation runs ...
    await store.async_save_bucket(entry.entry_id, bucket)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .bucket import BucketConfig, WaterBucket
from .const import STORAGE_KEY, STORAGE_VERSION

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

    async def async_load_bucket(
        self,
        entry_id: str,
        config:   BucketConfig,
    ) -> WaterBucket:
        """
        Load the persisted bucket for a zone, or create a fresh one.

        Parameters
        ----------
        entry_id : HA config entry ID for this zone.
        config   : BucketConfig from the current config entry.
                   Passed to ``WaterBucket.from_dict`` so that if the user
                   changed ``max_capacity`` since the last save, the loaded
                   level is correctly clamped to the new maximum.

        Returns
        -------
        WaterBucket  — restored from disk, or a fresh bucket at 50 % capacity.
        """
        data = await self._load_all()

        if entry_id in data:
            _LOGGER.debug(
                "Restoring bucket for entry %s: %s",
                entry_id, data[entry_id],
            )
            return WaterBucket.from_dict(data[entry_id], config)

        _LOGGER.debug(
            "No persisted state for entry %s — starting fresh bucket", entry_id
        )
        return WaterBucket(config)

    async def async_save_bucket(
        self,
        entry_id: str,
        bucket:   WaterBucket,
    ) -> None:
        """
        Persist the current bucket level for a zone.

        Reads the full file, updates only this zone's entry, then writes
        the whole file back. This prevents one zone's save from wiping
        another zone's data in a concurrent write scenario.

        Parameters
        ----------
        entry_id : HA config entry ID for this zone.
        bucket   : The WaterBucket whose state should be saved.
        """
        data = await self._load_all()
        data[entry_id] = bucket.to_dict()
        await self._store.async_save(data)
        _LOGGER.debug("Saved bucket for entry %s: %s", entry_id, bucket.to_dict())

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