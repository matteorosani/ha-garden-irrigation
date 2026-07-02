"""
Bucket state persistence.

Stores the bucket level and a minimal "pending watering" record per zone.
Sensor display values are handled by RestoreSensor (HA's state machine).
The pending record lets the coordinator correctly handle async_record_irrigation
after an HA restart — without it, the button would silently do nothing.

File layout
-----------
.storage/garden_irrigation
{
    "version": 1,
    "data": {
        "<entry_id>": {
            "level": 8.0,
            "pending": {
                "should_water": true,
                "water_mm": 17.6,
                "duration_minutes": 88.0,
                "volume_liters": 88.0
            }
        }
    }
}

"pending" is written after each calculation and cleared when
async_record_irrigation or async_reset_bucket is called.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .bucket import BucketConfig, WaterBucket
from .const import STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


@dataclass
class PendingWatering:
    """Minimal state needed to handle record_irrigation after a restart."""

    should_water: bool
    water_mm: float
    duration_minutes: float
    volume_liters: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_water": self.should_water,
            "water_mm": self.water_mm,
            "duration_minutes": self.duration_minutes,
            "volume_liters": self.volume_liters,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PendingWatering:
        return cls(
            should_water=bool(d["should_water"]),
            water_mm=float(d["water_mm"]),
            duration_minutes=float(d["duration_minutes"]),
            volume_liters=float(d["volume_liters"]),
        )


class IrrigationStore:
    """Persist bucket state and pending watering for all irrigation zones."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

    async def async_load_bucket(
        self,
        entry_id: str,
        config: BucketConfig,
    ) -> WaterBucket:
        """Load persisted bucket, or create a fresh one at 50%."""
        data = await self._load_all()
        zone_data = data.get(entry_id, {})

        if zone_data:
            bucket = WaterBucket.from_dict(zone_data, config)
            _LOGGER.debug(
                "store | zone=%s | restored bucket=%.1f mm (%.0f%%)",
                entry_id,
                bucket.level,
                bucket.percentage,
            )
            return bucket

        _LOGGER.debug(
            "store | zone=%s | no persisted state — fresh bucket",
            entry_id,
        )
        return WaterBucket(config)

    async def async_load_pending(
        self,
        entry_id: str,
    ) -> PendingWatering | None:
        """Load the pending watering record, if any."""
        data = await self._load_all()
        raw = data.get(entry_id, {}).get("pending")
        if not raw:
            return None
        try:
            pending = PendingWatering.from_dict(raw)
            _LOGGER.debug(
                "store | zone=%s | restored pending: should_water=%s  "
                "water_mm=%.1f  duration=%.1f min",
                entry_id,
                pending.should_water,
                pending.water_mm,
                pending.duration_minutes,
            )
            return pending
        except (KeyError, TypeError, ValueError) as exc:
            _LOGGER.debug(
                "store | zone=%s | could not restore pending (%s)",
                entry_id,
                exc,
            )
            return None

    async def async_save_bucket(
        self,
        entry_id: str,
        bucket: WaterBucket,
    ) -> None:
        """Persist bucket level, leaving any existing pending record untouched."""
        data = await self._load_all()
        zone_data = data.get(entry_id, {})
        zone_data.update(bucket.to_dict())
        data[entry_id] = zone_data
        await self._store.async_save(data)
        _LOGGER.debug(
            "store | zone=%s | saved level=%.1f mm",
            entry_id,
            bucket.level,
        )

    async def async_save_bucket_and_pending(
        self,
        entry_id: str,
        bucket: WaterBucket,
        pending: PendingWatering | None,
    ) -> None:
        """
        Persist bucket level and set (or clear) the pending watering record.

        Pass a PendingWatering to record that a watering is scheduled.
        Pass None to clear it (watering completed or bucket manually reset).
        """
        data = await self._load_all()
        zone_data = data.get(entry_id, {})
        zone_data.update(bucket.to_dict())

        if pending is None:
            zone_data.pop("pending", None)
            label = "cleared"
        else:
            zone_data["pending"] = pending.to_dict()
            label = f"{pending.duration_minutes:.0f} min pending"

        data[entry_id] = zone_data
        await self._store.async_save(data)
        _LOGGER.debug(
            "store | zone=%s | saved level=%.1f mm  pending=%s",
            entry_id,
            bucket.level,
            label,
        )

    async def async_remove_zone(self, entry_id: str) -> None:
        """Remove a zone's persisted state (called on unload)."""
        data = await self._load_all()
        if entry_id not in data:
            return
        del data[entry_id]
        if data:
            await self._store.async_save(data)
        else:
            await self._store.async_remove()
        _LOGGER.debug("store | zone=%s | removed", entry_id)

    async def _load_all(self) -> dict[str, Any]:
        data = await self._store.async_load()
        return data if isinstance(data, dict) else {}
