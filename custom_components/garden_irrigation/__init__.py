"""
Garden Irrigation - entry point and zone coordinator.

All homeassistant.* imports are lazy (inside function bodies or TYPE_CHECKING)
so that importing any submodule in unit tests does NOT trigger HA's deep
import chain.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import TYPE_CHECKING

from .bucket import BucketConfig, WaterBucket
from .const import (
    CONF_CALCULATION_TIME,
    CONF_CROPS,
    CONF_FLOW_RATE,
    CONF_MAX_BUCKET,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TARGET,
    CONF_PLANTING_DATE,
    CONF_ZONE_AREA,
    DOMAIN,
)
from .irrigator import IrrigationResult, ZoneConfig, calculate
from .kc import load_crops, set_user_crops_file
from .store import IrrigationStore, PendingWatering
from .weather.open_meteo import OpenMeteoProvider

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "button"]


def signal_update(entry_id: str) -> str:
    """Dispatcher signal string for coordinator → entity updates."""
    return f"{DOMAIN}_{entry_id}_update"


# -- HA lifecycle --------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one Garden Irrigation zone from a config entry."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    _LOGGER.debug("setup | zone=%s | starting", entry.title)

    hass.data.setdefault(DOMAIN, {})

    user_crops_path = os.path.join(
        hass.config.config_dir, "garden_irrigation_crops.json"
    )
    user_crops_loaded = await _async_setup_crops(hass, user_crops_path)
    _LOGGER.debug(
        "setup | zone=%s | user crops: %s",
        entry.title,
        "loaded" if user_crops_loaded else "not found (bundled only)",
    )

    data = {**entry.data, **entry.options}

    crop_ids = list(data[CONF_CROPS])
    max_capacity = float(data[CONF_MAX_BUCKET])

    # Derive threshold from crop sensitivity — most sensitive crop in zone wins
    from .kc import threshold_for_zone

    low_threshold, threshold_crop = threshold_for_zone(crop_ids, max_capacity)

    _LOGGER.info(
        "setup | zone=%s | threshold=%.1f mm (%.0f%% of %.0f mm, determined by %s)",
        entry.title,
        low_threshold,
        low_threshold / max_capacity * 100,
        max_capacity,
        threshold_crop,
    )

    zone_config = ZoneConfig(
        crop_ids=crop_ids,
        planting_date=date.fromisoformat(data[CONF_PLANTING_DATE]),
        zone_area_m2=float(data[CONF_ZONE_AREA]),
        flow_rate_lpm=float(data[CONF_FLOW_RATE]),
    )
    bucket_config = BucketConfig(
        max_capacity=max_capacity,
        low_threshold=low_threshold,
    )

    store = IrrigationStore(hass)
    bucket = await store.async_load_bucket(entry.entry_id, bucket_config)
    pending = await store.async_load_pending(entry.entry_id)

    _LOGGER.info(
        "setup | zone=%s | bucket=%.1f mm (%.0f%%)  pending=%s",
        entry.title,
        bucket.level,
        bucket.percentage,
        f"{pending.duration_minutes:.0f} min" if pending else "none",
    )

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
        pending=pending,
    )
    await coordinator.async_setup()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("setup | zone=%s | complete", entry.title)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and cancel its daily schedule."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coordinator: ZoneCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_teardown()
        _LOGGER.debug("setup | zone=%s | unloaded", entry.title)
    return ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when the user changes its options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_setup_crops(
    hass: HomeAssistant, user_crops_path: str | None = None
) -> bool:
    """
    Async entry point for loading crops — safe to call from the event loop.

    Runs the blocking file I/O in HA's executor thread pool so the event
    loop is never blocked. Should be called from ``async_setup_entry`` after
    ``set_user_crops_file()`` so the cache is warm before the config flow or
    coordinator ever need it.

    Parameters
    ----------
    hass            : HomeAssistant instance .
    user_crops_path : Path to the user-defined crops file, or None.

    Returns
    -------
    True if the user crops file was found and loaded, False otherwise.
    """
    set_user_crops_file(user_crops_path)

    # Run the blocking load in an executor thread
    await hass.async_add_executor_job(load_crops)  # type: ignore[attr-defined]

    user_file_loaded = user_crops_path is not None and os.path.exists(user_crops_path)

    if user_crops_path and not user_file_loaded:
        _LOGGER.debug(
            "garden_irrigation: no user crops file found at %s "
            "(create it to add custom crops)",
            user_crops_path,
        )
    elif user_file_loaded:
        _LOGGER.info("garden_irrigation: loaded user crops from %s", user_crops_path)

    return user_file_loaded


# ── Coordinator ────────────────────────────────────────────────────────────────


class ZoneCoordinator:
    """
    Owns the daily ET0 calculation for one irrigation zone.

    Updates are pushed to sensor entities via HA's dispatcher, which is the
    idiomatic HA approach for push-based sensor updates from a custom
    coordinator. Sensors subscribe in async_added_to_hass via
    async_dispatcher_connect and unsubscribe automatically via async_on_remove.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        bucket: WaterBucket,
        store: IrrigationStore,
        weather_provider: OpenMeteoProvider,
        zone_config: ZoneConfig,
        pending: PendingWatering | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._bucket = bucket
        self._store = store
        self._weather = weather_provider
        self._zone = zone_config
        self._pending = pending  # restored from store on startup
        self._last_result: IrrigationResult | None = None
        self._unsub: object = None
        # Set by record_irrigation / reset_bucket; cleared by the next calculation.
        # Allows sensors to show an up-to-date status immediately after a button
        # press without waiting for the next scheduled calculation.
        self._status_override: str | None = None

    # -- Properties ------------------------------------------------------------

    @property
    def bucket(self) -> WaterBucket:
        return self._bucket

    @property
    def last_result(self) -> IrrigationResult | None:
        return self._last_result

    @property
    def pending(self) -> PendingWatering | None:
        """Pending watering from last calculation, survives HA restarts."""
        return self._pending

    @property
    def watering_duration_minutes(self) -> float | None:
        """
        Duration the valve should open today [min].

        - Pending watering exists → pending duration (calculation said water)
        - Calculation ran, no pending → 0 (either completed or not needed)
        - No data at all → None (RestoreSensor will supply the previous value)
        """
        if self._pending is not None:
            return self._pending.duration_minutes
        if self._last_result is not None:
            return 0.0
        return None

    @property
    def status(self) -> str | None:
        """
        Human-readable status string for the status sensor.

        Priority: button override > fresh result > None (RestoreSensor fallback).
        """
        if self._status_override is not None:
            return self._status_override
        if self._last_result is None:
            return None  # RestoreSensor will supply the previous value
        if self._last_result.should_water:
            return "Water needed"
        from .irrigator import SKIP_BUCKET_SUFFICIENT, SKIP_FORECAST_RAIN_SUFFICIENT

        return {
            SKIP_BUCKET_SUFFICIENT: "Skipped: soil ok",
            SKIP_FORECAST_RAIN_SUFFICIENT: "Skipped: rain forecast",
        }.get(self._last_result.skip_reason or "", "Skipped")

    # -- Lifecycle -------------------------------------------------------------

    async def async_setup(self) -> None:
        """Register the daily calculation time trigger."""
        from homeassistant.helpers.event import async_track_time_change

        calc_time = self.entry.data[CONF_CALCULATION_TIME]
        hour, minute = (int(x) for x in calc_time.split(":"))

        self._unsub = async_track_time_change(
            self.hass,
            self._async_scheduled_run,
            hour=hour,
            minute=minute,
            second=0,
        )
        _LOGGER.debug(
            "coordinator | zone=%s | calculation scheduled at %02d:%02d",
            self.entry.title,
            hour,
            minute,
        )

    async def async_teardown(self) -> None:
        if self._unsub:
            self._unsub()  # type: ignore[operator]
            self._unsub = None

    # -- Daily calculation -----------------------------------------------------

    async def _async_scheduled_run(self, now: datetime) -> None:
        """
        Time trigger callback. async so HA calls it on the event loop
        rather than in a thread pool executor (Python 3.14+ requirement).
        """
        _LOGGER.debug(
            "coordinator | zone=%s | time trigger fired at %s",
            self.entry.title,
            now.strftime("%H:%M"),
        )
        self.hass.async_create_task(
            self._async_run(now.date()),
            name=f"{DOMAIN}_{self.entry.entry_id}_daily",
        )

    async def _async_run(self, today: date) -> None:
        """Full daily calculation cycle."""
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        # Clear any button-set status override — this calculation is authoritative
        self._status_override = None

        _LOGGER.info(
            "coordinator | zone=%s | calculation started for %s",
            self.entry.title,
            today,
        )

        # 1 - Weather
        try:
            weather = await self._weather.get_data()
            _LOGGER.debug(
                "coordinator | zone=%s | weather: Tmin=%.1f Tmax=%.1f "
                "rain_yesterday=%.1f mm  forecast=%.1f mm",
                self.entry.title,
                weather.temp_min,
                weather.temp_max,
                weather.precipitation_mm,
                weather.forecast_precip_mm,
            )
        except Exception as err:
            _LOGGER.error(
                "coordinator | zone=%s | weather fetch failed: %s",
                self.entry.title,
                err,
            )
            async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
            return

        # 2 - Calculate
        result = calculate(
            weather=weather,
            zone=self._zone,
            bucket=self._bucket,
            today=today,
            latitude_deg=self.hass.config.latitude,
        )
        self._last_result = result

        _LOGGER.info(
            "coordinator | zone=%s | ET0=%.2f mm  Kc=%.2f  "
            "rain=%.1f mm  bucket=%.1f mm (%.0f%%)  "
            "should_water=%s  duration=%.1f min  reason=%s",
            self.entry.title,
            result.et0_mm,
            result.kc,
            result.daily.rain_mm,
            result.bucket_level,
            result.bucket_percentage,
            result.should_water,
            result.duration_minutes,
            result.skip_reason or "n/a",
        )

        # 3 - Build pending record and persist
        pending = (
            PendingWatering(
                should_water=True,
                water_mm=result.water_mm,
                duration_minutes=result.duration_minutes,
                volume_liters=result.volume_liters,
            )
            if result.should_water
            else None
        )
        self._pending = pending
        await self._store.async_save_bucket_and_pending(
            self.entry.entry_id, self._bucket, pending
        )

        # 4 - Notify sensors via dispatcher
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

        # 5 - Optional push notification
        data = {**self.entry.data, **self.entry.options}
        if data.get(CONF_NOTIFY_ENABLED) and data.get(CONF_NOTIFY_TARGET):
            await self._async_notify(result, data[CONF_NOTIFY_TARGET])

    # -- Button actions --------------------------------------------------------

    async def async_trigger_calculation(self) -> None:
        """Trigger the ET0 calculation immediately (Calculate now button)."""
        _LOGGER.info(
            "coordinator | zone=%s | manual calculation requested",
            self.entry.title,
        )
        await self._async_run(date.today())

    async def async_record_irrigation(self) -> None:
        """
        Register that the automation completed a watering cycle.

        Works correctly after HA restarts because the pending record is
        restored from storage — coordinator.last_result doesn't need to
        be set for this to function.
        """
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        # Use pending (survives restarts) rather than last_result (in-memory only)
        pending = self._pending
        if pending is None or not pending.should_water:
            _LOGGER.warning(
                "coordinator | zone=%s | record_irrigation called but "
                "no pending watering — ignored",
                self.entry.title,
            )
            return

        self._bucket.add_irrigation(pending.water_mm)
        self._pending = None
        self._status_override = "Watered"
        await self._store.async_save_bucket_and_pending(
            self.entry.entry_id, self._bucket, None
        )
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

        _LOGGER.info(
            "coordinator | zone=%s | recorded %.1f mm irrigation — "
            "bucket now %.1f mm (%.0f%%)",
            self.entry.title,
            pending.water_mm,
            self._bucket.level,
            self._bucket.percentage,
        )

    async def async_reset_bucket(self) -> None:
        """Reset bucket to max capacity (manual watering or heavy rain)."""
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        self._bucket.reset()
        self._pending = None
        self._status_override = "Skipped: soil ok"
        await self._store.async_save_bucket_and_pending(
            self.entry.entry_id, self._bucket, None
        )
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

        _LOGGER.info(
            "coordinator | zone=%s | bucket reset to %.1f mm",
            self.entry.title,
            self._bucket.level,
        )

    # -- Notification ----------------------------------------------------------

    async def _async_notify(
        self,
        result: IrrigationResult,
        targets: list[str],
    ) -> None:
        """Send push notification(s) summarising the daily calculation."""
        zone = self.entry.title

        if result.should_water:
            title = f"💧 {zone}: water tomorrow"
            message = (
                f"Duration: {result.duration_minutes:.0f} min "
                f"({result.volume_liters:.0f} L)\n"
                f"Bucket: {result.bucket_percentage:.0f}% "
                f"({result.bucket_level:.1f} mm)\n"
                f"ET0: {result.et0_mm:.1f} mm · Kc: {result.kc:.2f}"
            )
        else:
            reasons = {
                "bucket_sufficient": "soil moisture ok",
                "forecast_rain_sufficient": "rain forecast",
            }
            reason = reasons.get(result.skip_reason or "", "skipped")
            title = f"🌿 {zone}: no watering needed ({reason})"
            message = (
                f"Bucket: {result.bucket_percentage:.0f}% "
                f"({result.bucket_level:.1f} mm)\n"
                f"ET0: {result.et0_mm:.1f} mm · Kc: {result.kc:.2f}\n"
                f"Rain yesterday: {result.daily.rain_mm:.1f} mm"
            )

        for target in targets:
            parts = target.split(".", 1)
            if len(parts) != 2:
                _LOGGER.warning(
                    "coordinator | zone=%s | invalid notify target '%s'",
                    self.entry.title,
                    target,
                )
                continue
            domain, service = parts
            try:
                await self.hass.services.async_call(
                    domain,
                    service,
                    {"title": title, "message": message},
                    blocking=False,
                )
                _LOGGER.debug(
                    "coordinator | zone=%s | notification sent via %s",
                    self.entry.title,
                    target,
                )
            except Exception as err:
                _LOGGER.warning(
                    "coordinator | zone=%s | notification to '%s' failed: %s",
                    self.entry.title,
                    target,
                    err,
                )
