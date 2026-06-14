"""
Garden Irrigation — entry point and zone coordinator.

Import discipline
-----------------
This file uses lazy HA imports deliberately:

  from __future__ import annotations        — all annotations become strings,
                                              so HA types in signatures are
                                              never evaluated at import time.

  if TYPE_CHECKING: ...                     — HA type imports only happen when
                                              a type-checker runs, not at runtime.

  HA helper imports inside function bodies  — async_get_clientsession,
                                              async_dispatcher_send, etc. are
                                              imported the first time the function
                                              is called, not when the package is
                                              imported.

This means `from garden_irrigation.et0 import et0_for_date` in a test file
does NOT trigger any HA imports, keeping unit tests fast and HA-free.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import TYPE_CHECKING

# ── Our own pure-Python modules (no HA dependency) ────────────────────────────
from .bucket import BucketConfig, WaterBucket
from .const import (
    CONF_CALCULATION_TIME,
    CONF_CROPS,
    CONF_FLOW_RATE,
    CONF_LOW_THRESHOLD,
    CONF_MAX_BUCKET,
    CONF_PLANTING_DATE,
    CONF_ZONE_AREA,
    DOMAIN,
)
from .irrigator import IrrigationResult, ZoneConfig, calculate
from .kc import set_user_crops_file
from .store import IrrigationStore
from .weather.open_meteo import OpenMeteoProvider

# ── HA types — for type-checkers only, never imported at runtime ───────────────
if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "button"]


def signal_update(entry_id: str) -> str:
    """Dispatcher signal string used by coordinator → entities."""
    return f"{DOMAIN}_{entry_id}_update"


# ── HA lifecycle ───────────────────────────────────────────────────────────────


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one Garden Irrigation zone from a config entry."""
    # HA helpers imported here (lazy) so the module can be imported without HA
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    hass.data.setdefault(DOMAIN, {})

    set_user_crops_file(
        os.path.join(hass.config.config_dir, "garden_irrigation_crops.json")
    )

    data = {**entry.data, **entry.options}

    zone_config = ZoneConfig(
        crop_ids=list(data[CONF_CROPS]),
        planting_date=date.fromisoformat(data[CONF_PLANTING_DATE]),
        zone_area_m2=float(data[CONF_ZONE_AREA]),
        flow_rate_lpm=float(data[CONF_FLOW_RATE]),
    )
    bucket_config = BucketConfig(
        max_capacity=float(data[CONF_MAX_BUCKET]),
        low_threshold=float(data[CONF_LOW_THRESHOLD]),
    )

    store = IrrigationStore(hass)
    bucket = await store.async_load_bucket(entry.entry_id, bucket_config)

    provider = OpenMeteoProvider(
        latitude=hass.config.latitude,
        longitude=hass.config.longitude,
        session=async_get_clientsession(hass),
    )

    coordinator = ZoneCoordinator(
        hass=hass,
        entry=entry,
        bucket=bucket,
        store=store,
        weather_provider=provider,
        zone_config=zone_config,
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
    """Owns the daily calculation for one irrigation zone."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        bucket: WaterBucket,
        store: IrrigationStore,
        weather_provider: OpenMeteoProvider,
        zone_config: ZoneConfig,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._bucket = bucket
        self._store = store
        self._weather = weather_provider
        self._zone = zone_config
        self._last_result: IrrigationResult | None = None
        self._unsub: object = None

    @property
    def bucket(self) -> WaterBucket:
        return self._bucket

    @property
    def last_result(self) -> IrrigationResult | None:
        return self._last_result

    async def async_setup(self) -> None:
        """Register the daily time trigger."""
        from homeassistant.helpers.event import async_track_time_change

        calculation_time = self.entry.data[CONF_CALCULATION_TIME]
        hour, minute = (int(x) for x in calculation_time.split(":"))

        self._unsub = async_track_time_change(
            self.hass,
            self._async_scheduled_run,
            hour=hour,
            minute=minute,
            second=0,
        )

    async def async_teardown(self) -> None:
        """Cancel the daily schedule."""
        if self._unsub:
            self._unsub()  # type: ignore[operator]
            self._unsub = None

    def _async_scheduled_run(self, now: datetime) -> None:
        """Time trigger callback — hand off to a task immediately."""
        self.hass.async_create_task(
            self._async_run(now.date()),
            name=f"{DOMAIN}_{self.entry.entry_id}_daily",
        )

    async def _async_run(self, today: date) -> None:
        """Daily calculation cycle."""
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        try:
            weather = await self._weather.get_data()
        except Exception as err:
            _LOGGER.error(
                "Zone '%s': weather fetch failed, skipping today — %s",
                self.entry.title,
                err,
            )
            return

        result = calculate(
            weather=weather,
            zone=self._zone,
            bucket=self._bucket,
            today=today,
            latitude_deg=self.hass.config.latitude,
        )
        self._last_result = result

        _LOGGER.info(
            "Zone '%s': ET₀=%.2f  Kc=%.2f  rain=%.1f mm  "
            "bucket=%.1f mm (%.0f%%)  should_water=%s  duration=%.1f min  reason=%s",
            self.entry.title,
            result.et0_mm,
            result.kc,
            result.daily.rain_mm,
            result.bucket_level,
            result.bucket_percentage,
            result.should_water,
            result.duration_minutes,
            result.skip_reason or "—",
        )

        await self._store.async_save_bucket(self.entry.entry_id, self._bucket)
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    async def async_record_irrigation(self) -> None:
        """Register that the automation completed a watering cycle."""
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        if self._last_result is None or not self._last_result.should_water:
            _LOGGER.warning(
                "Zone '%s': record_irrigation called but no pending water need - ignored",  # noqa: E501
                self.entry.title,
            )
            return

        self._bucket.add_irrigation(self._last_result.water_mm)
        await self._store.async_save_bucket(self.entry.entry_id, self._bucket)
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

        _LOGGER.info(
            "Zone '%s': recorded %.1f mm - bucket now %.1f mm (%.0f%%)",
            self.entry.title,
            self._last_result.water_mm,
            self._bucket.level,
            self._bucket.percentage,
        )

    async def async_reset_bucket(self) -> None:
        """Reset bucket to max capacity."""
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        self._bucket.reset()
        await self._store.async_save_bucket(self.entry.entry_id, self._bucket)
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
        _LOGGER.info(
            "Zone '%s': bucket manually reset to %.1f mm",
            self.entry.title,
            self._bucket.level,
        )
