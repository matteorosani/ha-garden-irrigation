"""
Button platform — Garden Irrigation.

Provides a single button per zone: "Reset bucket".

Pressing it calls ``coordinator.async_reset_bucket()``, which sets the
soil water level back to maximum capacity. Use this after watering manually
or after a heavy rain that wasn't captured by the weather provider.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from propcache import cached_property

from . import ZoneCoordinator
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create button entities for this zone."""
    coordinator: ZoneCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ResetBucketButton(coordinator, entry),
        RecordIrrigationButton(coordinator, entry),
    ])


class ResetBucketButton(ButtonEntity):
    """
    Button that resets the zone's water bucket to full capacity.

    Appears in the HA UI under the zone's device. Also callable from
    automations via ``button.press``, e.g. after a rain sensor fires.
    """

    _attr_has_entity_name = True
    _attr_icon            = "mdi:bucket-outline"

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry       = entry
        self._attr_unique_id = f"{entry.entry_id}_reset_bucket"
        self._attr_name      = "Reset bucket"

    @cached_property
    def device_info(self) -> DeviceInfo:
        """Group this button under the same device as the sensors."""
        return DeviceInfo(
            identifiers = {(DOMAIN, self._entry.entry_id)},
            name        = self._entry.title,
            manufacturer= "Garden Irrigation",
            model       = "Drip Irrigation Zone",
        )

    async def async_press(self) -> None:
        """Called when the user presses the button in the UI or via automation."""
        await self._coordinator.async_reset_bucket()


class RecordIrrigationButton(ButtonEntity):
    """
    Button that tells the integration "watering just completed".

    Call this from your automation after the valve closes:

        - service: button.press
          target: {entity_id: button.vegetable_bed_record_irrigation}

    This adds the calculated water amount to the bucket model so the
    next daily calculation starts from the correct soil moisture level.
    It is safe to call from automations, scripts, or the UI.
    """

    _attr_has_entity_name = True
    _attr_icon            = "mdi:check-circle-outline"

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry       = entry
        self._attr_unique_id = f"{entry.entry_id}_record_irrigation"
        self._attr_name      = "Record irrigation"

    @cached_property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers  = {(DOMAIN, self._entry.entry_id)},
            name         = self._entry.title,
            manufacturer = "Garden Irrigation",
            model        = "Drip Irrigation Zone",
        )

    async def async_press(self) -> None:
        """Register that the automation completed a watering cycle."""
        await self._coordinator.async_record_irrigation()