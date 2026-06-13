"""
Sensor platform — Garden Irrigation.

Exposes the irrigation state for one zone as Home Assistant sensor entities.
All sensors are push-based (should_poll = False): the ZoneCoordinator
dispatches a signal after each daily run and the entities write their new
state immediately.

Entities created per zone
--------------------------
  Bucket Level        — current soil water level [mm]
  Bucket Percentage   — level as % of max capacity
  ET₀ Today           — reference evapotranspiration [mm/day]
  Crop Coefficient    — effective Kc for today's growth stage
  Watering Duration   — scheduled valve open time today [min]
  Rain Yesterday      — measured precipitation from yesterday [mm]
  Status              — human-readable summary of today's decision
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from propcache import cached_property

from . import ZoneCoordinator, signal_update
from .const import DOMAIN
from .irrigator import SKIP_BUCKET_SUFFICIENT, SKIP_FORECAST_RAIN_SUFFICIENT


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create all sensor entities for this zone."""
    coordinator: ZoneCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([
        BucketLevelSensor(coordinator, entry),
        BucketPercentageSensor(coordinator, entry),
        Et0Sensor(coordinator, entry),
        KcSensor(coordinator, entry),
        WateringDurationSensor(coordinator, entry),
        RainYesterdaySensor(coordinator, entry),
        StatusSensor(coordinator, entry),
    ])


# ── Base class ─────────────────────────────────────────────────────────────────

class ZoneSensorBase(SensorEntity):
    """
    Common base for all Garden Irrigation sensors.

    Handles device grouping, dispatcher subscription, and the push-based
    state update pattern. Concrete subclasses only need to implement
    ``native_value`` and optionally ``extra_state_attributes``.
    """

    _attr_has_entity_name = True   # name is relative to the device
    _attr_should_poll     = False  # coordinator pushes updates

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry       = entry

    # ── Device grouping ────────────────────────────────────────────────────────

    @cached_property
    def device_info(self) -> DeviceInfo:
        """All sensors for this zone appear under the same HA device."""
        return DeviceInfo(
            identifiers = {(DOMAIN, self._entry.entry_id)},
            name        = self._entry.title,
            manufacturer= "Garden Irrigation",
            model       = "Drip Irrigation Zone",
            entry_type  = None,
        )

    # ── Dispatcher subscription ────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates when entity is added to HA."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self._entry.entry_id),
                self._handle_coordinator_update,
            )
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Called by the dispatcher; tells HA to re-read native_value."""
        self.async_write_ha_state()


# ── Concrete sensors ───────────────────────────────────────────────────────────

class BucketLevelSensor(ZoneSensorBase):
    """Current soil water level in mm."""

    _attr_icon                    = "mdi:pail"
    _attr_native_unit_of_measurement = "mm"
    _attr_state_class             = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_bucket_level"
        self._attr_name      = "Bucket level"

    @cached_property
    def native_value(self) -> float | None:
        return round(self._coordinator.bucket.level, 1)

    @cached_property
    def extra_state_attributes(self) -> dict[str, Any]:
        bucket = self._coordinator.bucket
        return {
            "max_capacity_mm":  bucket.config.max_capacity,
            "low_threshold_mm": bucket.config.low_threshold,
            "deficit_mm":       round(bucket.deficit_mm, 1),
            "needs_water":      bucket.needs_water,
        }


class BucketPercentageSensor(ZoneSensorBase):
    """Bucket level as a percentage of maximum capacity. Useful for gauge cards."""

    _attr_icon                    = "mdi:gauge"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class             = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_bucket_percentage"
        self._attr_name      = "Bucket percentage"

    @cached_property
    def native_value(self) -> float | None:
        return self._coordinator.bucket.percentage


class Et0Sensor(ZoneSensorBase):
    """Reference evapotranspiration ET₀ computed for today [mm/day]."""

    _attr_icon                    = "mdi:weather-sunny"
    _attr_native_unit_of_measurement = "mm/day"
    _attr_state_class             = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_et0"
        self._attr_name      = "ET₀ today"

    @cached_property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        if result is None:
            return None
        return result.et0_mm

    @cached_property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.last_result
        if result is None:
            return {}
        return {
            "kc":          result.kc,
            "et_crop_mm":  result.et_crop_mm,
        }


class KcSensor(ZoneSensorBase):
    """
    Effective crop coefficient for today.

    Shows the averaged Kc across all crops in the zone, interpolated
    to the current growth stage based on the planting date.
    """

    _attr_icon        = "mdi:leaf"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_kc"
        self._attr_name      = "Crop coefficient (Kc)"

    @cached_property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        if result is None:
            return None
        return result.kc


class WateringDurationSensor(ZoneSensorBase):
    """
    Planned (or last executed) valve open time for today [min].

    Returns 0 when watering was skipped. The ``skip_reason`` attribute
    explains why, e.g. "bucket_sufficient" or "forecast_rain_sufficient".
    """

    _attr_icon                    = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "min"
    _attr_state_class             = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_watering_duration"
        self._attr_name      = "Watering duration"

    @cached_property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        if result is None:
            return None
        return result.duration_minutes

    @cached_property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.last_result
        if result is None:
            return {}
        attrs: dict[str, Any] = {
            "water_mm":      result.water_mm,
            "volume_liters": result.volume_liters,
            "should_water":  result.should_water,
        }
        if result.skip_reason:
            attrs["skip_reason"] = _skip_reason_label(result.skip_reason)
        return attrs


class RainYesterdaySensor(ZoneSensorBase):
    """Measured precipitation from yesterday [mm], used to update the bucket."""

    _attr_icon                    = "mdi:weather-rainy"
    _attr_device_class            = SensorDeviceClass.PRECIPITATION
    _attr_native_unit_of_measurement = "mm"
    _attr_state_class             = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rain_yesterday"
        self._attr_name      = "Rain yesterday"

    @cached_property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        if result is None:
            return None
        return result.daily.rain_mm

    @cached_property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.last_result
        if result is None:
            return {}
        return {
            "net_change_mm":  result.daily.net_change,
            "was_clamped":    result.daily.was_clamped,
        }


class StatusSensor(ZoneSensorBase):
    """
    Human-readable summary of today's irrigation decision.

    Possible values:
      "Water needed"       — duration > 0, waiting for automation to run valve
      "Skipped: rain"      — forecast rain is sufficient
      "Skipped: soil ok"   — bucket above threshold, no watering needed
      "Idle"               — no daily result yet (before first run)
    """

    _attr_icon = "mdi:sprinkler"

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_name      = "Status"

    @cached_property
    def native_value(self) -> str:
        result = self._coordinator.last_result
        if result is None:
            return "Idle"
        if result.should_water:
            return "Water needed"
        return _skip_reason_label(result.skip_reason or "")

    @cached_property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.last_result
        if result is None:
            return {}
        return {
            "bucket_level_mm":    result.bucket_level,
            "bucket_percent":     result.bucket_percentage,
            "et0_mm":             result.et0_mm,
            "kc":                 result.kc,
            "duration_minutes":   result.duration_minutes,
        }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _skip_reason_label(reason: str) -> str:
    """Convert internal skip reason constants to dashboard-friendly strings."""
    return {
        SKIP_BUCKET_SUFFICIENT:        "Skipped: soil ok",
        SKIP_FORECAST_RAIN_SUFFICIENT: "Skipped: rain forecast",
    }.get(reason, "Skipped")