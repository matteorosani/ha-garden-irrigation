"""
Sensor platform - Garden Irrigation.

All sensors share a single base class that:
  - Groups entities under the zone device
  - Subscribes to dispatcher updates (the HA-idiomatic push pattern)
  - Inherits RestoreSensor so the last known value survives HA restarts

RestoreSensor works via HA's state machine: on startup it calls
async_get_last_sensor_data() which returns the last written state.
Each native_value property checks coordinator.last_result first (fresh
calculation data) and falls back to the restored _attr_native_value when
the coordinator hasn't run yet since startup.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZoneCoordinator, signal_update
from .const import DOMAIN
from .irrigator import SKIP_BUCKET_SUFFICIENT, SKIP_FORECAST_RAIN_SUFFICIENT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create all sensor entities for this zone."""
    coordinator: ZoneCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            BucketLevelSensor(coordinator, entry),
            BucketPercentageSensor(coordinator, entry),
            Et0Sensor(coordinator, entry),
            KcSensor(coordinator, entry),
            WateringDurationSensor(coordinator, entry),
            RainYesterdaySensor(coordinator, entry),
            StatusSensor(coordinator, entry),
        ]
    )


# -- Base class ----------------------------------------------------------------


class ZoneSensorBase(RestoreSensor):
    """
    Common base for all Garden Irrigation sensors.

    Inherits RestoreSensor so every sensor's last known value survives
    HA restarts. The dispatcher subscription pushes updates from the
    coordinator whenever a calculation completes or a button is pressed.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Garden Irrigation",
            model="Drip Irrigation Zone",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to dispatcher updates and restore previous state."""
        await super().async_added_to_hass()  # RestoreSensor chain

        # Restore the last known native_value from HA's state machine
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value
            _LOGGER.debug(
                "sensor | %s | restored value: %s",
                self.entity_id,
                last.native_value,
            )

        # Subscribe to coordinator push updates
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self._entry.entry_id),
                self._handle_update,
            )
        )
        _LOGGER.debug("sensor | %s | subscribed to dispatcher", self.entity_id)

    @callback
    def _handle_update(self) -> None:
        """Dispatcher callback — write new state to HA."""
        _LOGGER.debug("sensor | %s | dispatcher update received", self.entity_id)
        self.async_write_ha_state()


# -- Concrete sensors ----------------------------------------------------------


class BucketLevelSensor(ZoneSensorBase):
    """Current soil water level [mm]. Always live from the bucket object."""

    _attr_icon = "mdi:pail"
    _attr_native_unit_of_measurement = "mm"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_bucket_level"
        self._attr_name = "Bucket level"

    @property
    def native_value(self) -> float:
        # Always live — bucket is restored from storage on startup
        return round(self._coordinator.bucket.level, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        b = self._coordinator.bucket
        return {
            "max_capacity_mm": b.config.max_capacity,
            "low_threshold_mm": b.config.low_threshold,
            "deficit_mm": round(b.deficit_mm, 1),
            "needs_water": b.needs_water,
        }


class BucketPercentageSensor(ZoneSensorBase):
    """Bucket level as % of max capacity."""

    _attr_icon = "mdi:gauge"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_bucket_percentage"
        self._attr_name = "Bucket percentage"

    @property
    def native_value(self) -> float:
        return self._coordinator.bucket.percentage


class Et0Sensor(ZoneSensorBase):
    """Reference ET0 for today [mm/day]."""

    _attr_icon = "mdi:weather-sunny"
    _attr_native_unit_of_measurement = "mm/day"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_et0"
        self._attr_name = "ET\u2080 today"

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        if result is not None:
            self._attr_native_value = result.et0_mm
        return self._attr_native_value  # type: ignore[return-value]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.last_result
        if result is None:
            return {}
        return {"kc": result.kc, "et_crop_mm": result.et_crop_mm}


class KcSensor(ZoneSensorBase):
    """Effective crop coefficient for today's growth stage."""

    _attr_icon = "mdi:leaf"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_kc"
        self._attr_name = "Crop coefficient (Kc)"

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        if result is not None:
            self._attr_native_value = result.kc
        return self._attr_native_value  # type: ignore[return-value]


class WateringDurationSensor(ZoneSensorBase):
    """
    Recommended valve open time today [min].

    Falls back to the value restored by RestoreSensor so the morning
    automation can read a valid duration even after an HA restart.
    """

    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_watering_duration"
        self._attr_name = "Watering duration"

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        if result is not None:
            self._attr_native_value = result.duration_minutes
        return self._attr_native_value  # type: ignore[return-value]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.last_result
        if result is None:
            return {}
        attrs: dict[str, Any] = {
            "water_mm": result.water_mm,
            "volume_liters": result.volume_liters,
            "should_water": result.should_water,
        }
        if result.skip_reason:
            attrs["skip_reason"] = _skip_reason_label(result.skip_reason)
        return attrs


class RainYesterdaySensor(ZoneSensorBase):
    """Measured precipitation from yesterday [mm]."""

    _attr_icon = "mdi:weather-rainy"
    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_native_unit_of_measurement = "mm"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rain_yesterday"
        self._attr_name = "Rain yesterday"

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        if result is not None:
            self._attr_native_value = result.daily.rain_mm
        return self._attr_native_value  # type: ignore[return-value]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.last_result
        if result is None:
            return {}
        return {
            "net_change_mm": result.daily.net_change,
            "was_clamped": result.daily.was_clamped,
        }


class StatusSensor(ZoneSensorBase):
    """Human-readable summary of today's irrigation decision."""

    _attr_icon = "mdi:sprinkler"

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_name = "Status"

    @property
    def native_value(self) -> str:
        result = self._coordinator.last_result
        if result is None:
            # No fresh result — return restored value or Idle
            return self._attr_native_value or "Idle"  # type: ignore[return-value]
        status = (
            "Water needed"
            if result.should_water
            else _skip_reason_label(result.skip_reason or "")
        )
        self._attr_native_value = status
        return status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.last_result
        if result is None:
            return {}
        return {
            "bucket_level_mm": result.bucket_level,
            "bucket_percent": result.bucket_percentage,
            "et0_mm": result.et0_mm,
            "kc": result.kc,
            "duration_minutes": result.duration_minutes,
        }


# -- Helpers -------------------------------------------------------------------


def _skip_reason_label(reason: str) -> str:
    return {
        SKIP_BUCKET_SUFFICIENT: "Skipped: soil ok",
        SKIP_FORECAST_RAIN_SUFFICIENT: "Skipped: rain forecast",
    }.get(reason, "Skipped")
