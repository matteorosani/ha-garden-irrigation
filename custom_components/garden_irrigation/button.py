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
    async_add_entities(
        [
            ResetBucketButton(coordinator, entry),
            RecordIrrigationButton(coordinator, entry),
            CalculateNowButton(coordinator, entry),
        ]
    )


# ── Shared base ────────────────────────────────────────────────────────────────


class _ZoneButton(ButtonEntity):
    """Common base: device grouping and constructor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry

    @cached_property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Garden Irrigation",
            model="Drip Irrigation Zone",
        )


# ── Concrete buttons ───────────────────────────────────────────────────────────


class ResetBucketButton(_ZoneButton):
    """
    Reset the bucket to full capacity.

    Use after manual watering or after heavy rain that the weather provider
    didn't capture, so the model reflects a fully saturated soil.
    """

    _attr_icon = "mdi:bucket-outline"

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_reset_bucket"
        self._attr_name = "Reset bucket"

    async def async_press(self) -> None:
        """Called when the user presses the button in the UI or via automation."""
        await self._coordinator.async_reset_bucket()


class RecordIrrigationButton(_ZoneButton):
    """
    Button that tells the integration "watering just completed".

    Call this from your automation after the valve closes:

        - service: button.press
          target: {entity_id: button.zone_record_irrigation}

    This adds the calculated water amount to the bucket model so the
    next daily calculation starts from the correct soil moisture level.
    It is safe to call from automations, scripts, or the UI.
    """

    _attr_icon = "mdi:check-circle-outline"

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_record_irrigation"
        self._attr_name = "Record irrigation"

    async def async_press(self) -> None:
        """Register that the automation completed a watering cycle."""
        await self._coordinator.async_record_irrigation()


class CalculateNowButton(_ZoneButton):
    """
    Trigger the ET₀ calculation immediately.

    Normally the calculation runs once a day at the configured time (23:00
    by default). Press this button to run it right now — useful when:
      - You just installed the integration and want to see sensor values
        without waiting until tonight
      - You changed zone settings and want to verify the new numbers
      - You are debugging the system

    The calculation fetches live weather data, updates the bucket, and
    writes all sensor values exactly as the scheduled run would.
    """

    _attr_icon = "mdi:calculator-variant-outline"

    def __init__(self, coordinator: ZoneCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_calculate_now"
        self._attr_name = "Calculate now"

    async def async_press(self) -> None:
        await self._coordinator.async_trigger_calculation()
