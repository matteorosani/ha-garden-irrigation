"""
Garden Irrigation — entry point and zone coordinator.

Architecture
------------
This integration follows the HA principle of separation of concerns:

  Integration  →  calculates water need, exposes sensor data
  Automation   →  reads the sensors, controls the valve, calls record button

The coordinator never touches a valve entity. It runs a daily calculation,
updates sensors, and waits for the user's automation to report back that
watering happened (via the "Record irrigation" button entity).

This keeps the integration flexible: the automation can add any extra
conditions (wind speed, presence, a local rain sensor) without touching
the integration code.

Recommended automation pattern
-------------------------------
    trigger:
      - platform: time
        at: "07:00:00"          # same time as CONF_WATERING_TIME
    condition:
      - condition: numeric_state
        entity_id: sensor.vegetable_bed_watering_duration
        above: 0
    action:
      - service: switch.turn_on
        target: {entity_id: switch.valve_zone_1}
      - delay:
          minutes: "{{ states('sensor.vegetable_bed_watering_duration') | int }}"
      - service: switch.turn_off
        target: {entity_id: switch.valve_zone_1}
      - service: button.press
        target: {entity_id: button.vegetable_bed_record_irrigation}
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change

from .bucket import BucketConfig, WaterBucket
from .const import (
    CONF_CROPS,
    CONF_FLOW_RATE,
    CONF_LOW_THRESHOLD,
    CONF_MAX_BUCKET,
    CONF_PLANTING_DATE,
    CONF_WATERING_TIME,
    CONF_ZONE_AREA,
    DOMAIN,
)
from .irrigator import IrrigationResult, ZoneConfig, calculate
from .store import IrrigationStore
from .weather.open_meteo import OpenMeteoProvider

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "button"]


def signal_update(entry_id: str) -> str:
    """Dispatcher signal string used by coordinator → entities."""
    return f"{DOMAIN}_{entry_id}_update"


# ── HA lifecycle ───────────────────────────────────────────────────────────────

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one Garden Irrigation zone from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Merge data + options so that options flow edits take effect on reload
    data = {**entry.data, **entry.options}

    zone_config = ZoneConfig(
        crop_ids      = list(data[CONF_CROPS]),
        planting_date = date.fromisoformat(data[CONF_PLANTING_DATE]),
        zone_area_m2  = float(data[CONF_ZONE_AREA]),
        flow_rate_lpm = float(data[CONF_FLOW_RATE]),
    )
    bucket_config = BucketConfig(
        max_capacity  = float(data[CONF_MAX_BUCKET]),
        low_threshold = float(data[CONF_LOW_THRESHOLD]),
    )

    store  = IrrigationStore(hass)
    bucket = await store.async_load_bucket(entry.entry_id, bucket_config)

    provider = OpenMeteoProvider(
        latitude  = hass.config.latitude,
        longitude = hass.config.longitude,
        session   = async_get_clientsession(hass),
    )

    coordinator = ZoneCoordinator(
        hass             = hass,
        entry            = entry,
        bucket           = bucket,
        store            = store,
        weather_provider = provider,
        zone_config      = zone_config,
    )
    await coordinator.async_setup()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("Garden Irrigation: zone '%s' loaded", entry.title)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and cancel its daily schedule."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coordinator: ZoneCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_teardown()
    return ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when the user changes its options."""
    await hass.config_entries.async_reload(entry.entry_id)


# ── Coordinator ────────────────────────────────────────────────────────────────

class ZoneCoordinator:
    """
    Owns the daily calculation for one irrigation zone.

    Responsibilities
    ----------------
    - Schedules a daily run at the configured time.
    - Fetches weather from Open-Meteo.
    - Calls calculate() → IrrigationResult.
    - Persists the updated bucket to disk.
    - Dispatches a signal so sensor entities refresh.

    NOT a responsibility
    --------------------
    - Opening or closing valves. That belongs in the user's automation.
      Call async_record_irrigation() from the automation after the valve
      closes to register delivered water in the bucket.

    Attributes read by entity platforms
    ------------------------------------
    bucket       : WaterBucket  — current level, percentage, deficit
    last_result  : IrrigationResult | None  — today's calculation output
    """

    def __init__(
        self,
        hass:             HomeAssistant,
        entry:            ConfigEntry,
        bucket:           WaterBucket,
        store:            IrrigationStore,
        weather_provider: OpenMeteoProvider,
        zone_config:      ZoneConfig,
    ) -> None:
        self.hass    = hass
        self.entry   = entry
        self._bucket = bucket
        self._store  = store
        self._weather = weather_provider
        self._zone   = zone_config
        self._last_result: IrrigationResult | None = None
        self._unsub:       CALLBACK_TYPE | None = None

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def bucket(self) -> WaterBucket:
        return self._bucket

    @property
    def last_result(self) -> IrrigationResult | None:
        return self._last_result

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Register the daily time trigger."""
        watering_time = self.entry.data[CONF_WATERING_TIME]
        hour, minute  = (int(x) for x in watering_time.split(":"))

        self._unsub = async_track_time_change(
            self.hass,
            self._async_scheduled_run,
            hour=hour, minute=minute, second=0,
        )
        _LOGGER.debug(
            "Zone '%s': daily calculation scheduled at %02d:%02d",
            self.entry.title, hour, minute,
        )

    async def async_teardown(self) -> None:
        """Cancel the daily schedule."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    # ── Daily calculation ──────────────────────────────────────────────────────

    @callback
    def _async_scheduled_run(self, now: datetime) -> None:
        """Time trigger callback — hand off to a task immediately."""
        self.hass.async_create_task(
            self._async_run(now.date()),
            name=f"{DOMAIN}_{self.entry.entry_id}_daily",
        )

    async def _async_run(self, today: date) -> None:
        """
        Daily calculation cycle.

        1. Fetch weather (abort on failure — don't update bucket with bad data).
        2. Run calculate() — updates bucket with ET₀ deduction and rain.
        3. Persist updated bucket.
        4. Notify sensor entities.

        The automation is responsible for opening/closing the valve and
        calling async_record_irrigation() afterwards.
        """
        _LOGGER.debug("Zone '%s': daily run for %s", self.entry.title, today)

        try:
            weather = await self._weather.get_data()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Zone '%s': weather fetch failed, skipping today — %s",
                self.entry.title, err,
            )
            return

        result = calculate(
            weather      = weather,
            zone         = self._zone,
            bucket       = self._bucket,
            today        = today,
            latitude_deg = self.hass.config.latitude,
        )
        self._last_result = result

        _LOGGER.info(
            "Zone '%s': ET₀=%.2f  Kc=%.2f  rain=%.1f mm  "
            "bucket=%.1f mm (%.0f%%)  should_water=%s  duration=%.1f min  reason=%s",
            self.entry.title,
            result.et0_mm, result.kc,
            result.daily.rain_mm,
            result.bucket_level, result.bucket_percentage,
            result.should_water, result.duration_minutes,
            result.skip_reason or "—",
        )

        await self._store.async_save_bucket(self.entry.entry_id, self._bucket)
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    # ── Called by button entities ──────────────────────────────────────────────

    async def async_record_irrigation(self) -> None:
        """
        Register that the automation completed a watering cycle.

        Call this from your automation after the valve closes:

            - service: button.press
              target: {entity_id: button.vegetable_bed_record_irrigation}

        This adds last_result.water_mm to the bucket and persists the new
        level so the next calculation starts from the correct baseline.
        """
        if self._last_result is None or not self._last_result.should_water:
            _LOGGER.warning(
                "Zone '%s': record_irrigation called but no pending water need — ignored",
                self.entry.title,
            )
            return

        self._bucket.add_irrigation(self._last_result.water_mm)
        await self._store.async_save_bucket(self.entry.entry_id, self._bucket)
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

        _LOGGER.info(
            "Zone '%s': recorded %.1f mm irrigation — bucket now %.1f mm (%.0f%%)",
            self.entry.title,
            self._last_result.water_mm,
            self._bucket.level,
            self._bucket.percentage,
        )

    async def async_reset_bucket(self) -> None:
        """Reset bucket to max capacity (manual full watering or after heavy rain)."""
        self._bucket.reset()
        await self._store.async_save_bucket(self.entry.entry_id, self._bucket)
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
        _LOGGER.info(
            "Zone '%s': bucket manually reset to %.1f mm",
            self.entry.title, self._bucket.level,
        )