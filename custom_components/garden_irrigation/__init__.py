"""
Garden Irrigation — entry point and zone coordinator.

Import discipline
-----------------
All homeassistant.* imports are lazy (inside function bodies or guarded by
TYPE_CHECKING) so that importing any submodule of this package in unit tests
does NOT trigger HA's deep import chain.
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
    CONF_LOW_THRESHOLD,
    CONF_MAX_BUCKET,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TARGET,
    CONF_PLANTING_DATE,
    CONF_ZONE_AREA,
    DOMAIN,
)
from .irrigator import IrrigationResult, ZoneConfig, calculate
from .kc import load_crops, set_user_crops_file
from .store import IrrigationStore
from .weather.open_meteo import OpenMeteoProvider

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
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    hass.data.setdefault(DOMAIN, {})

    # Load crops in an executor thread (file I/O must not block the event loop).
    # _async_setup_crops also sets the user crops path and logs whether the file
    # was found, giving the user a clear signal about where to put their crops.
    user_crops_path = os.path.join(
        hass.config.config_dir, "garden_irrigation_crops.json"
    )
    user_crops_loaded = await _async_setup_crops(hass, user_crops_path)
    hass.data.setdefault(f"{DOMAIN}_user_crops_loaded", {})[entry.entry_id] = (
        user_crops_loaded
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

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Register the daily calculation trigger."""
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
            "Zone '%s': daily calculation scheduled at %02d:%02d",
            self.entry.title,
            hour,
            minute,
        )

    async def async_teardown(self) -> None:
        """Cancel the daily schedule."""
        if self._unsub:
            self._unsub()  # type: ignore[operator]
            self._unsub = None

    # ── Daily cycle ────────────────────────────────────────────────────────────

    async def _async_scheduled_run(self, now: datetime) -> None:
        """Time trigger callback — hand off to a task immediately."""
        self.hass.async_create_task(
            self._async_run(now.date()),
            name=f"{DOMAIN}_{self.entry.entry_id}_daily",
        )

    async def _async_run(self, today: date) -> None:
        """
        Daily calculation cycle.

        Runs at CONF_CALCULATION_TIME (recommended: 23:00).
        Updates the bucket model, persists state, notifies entities,
        and optionally pushes a summary notification to the user.
        """
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        _LOGGER.debug("Zone '%s': daily calculation for %s", self.entry.title, today)

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

        # Optional push notification
        data = {**self.entry.data, **self.entry.options}
        if data.get(CONF_NOTIFY_ENABLED) and data.get(CONF_NOTIFY_TARGET):
            await self._async_notify(result, data[CONF_NOTIFY_TARGET])

    async def _async_notify(self, result: IrrigationResult, targets: list[str]) -> None:
        """
        Send a push notification to one or more notify services.

        ``targets`` is a list of service strings from the multi-select (e.g.
        ["notify.mobile_app_alice", "notify.mobile_app_bob"]).
        """
        zone = self.entry.title

        if result.should_water:
            title = f"💧 {zone}: water tomorrow"
            message = (
                f"Duration: {result.duration_minutes:.0f} min "
                f"({result.volume_liters:.0f} L)\n"
                f"Bucket: {result.bucket_percentage:.0f}% "
                f"({result.bucket_level:.1f} mm)\n"
                f"ET₀: {result.et0_mm:.1f} mm · Kc: {result.kc:.2f}"
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
                f"ET₀: {result.et0_mm:.1f} mm · Kc: {result.kc:.2f}\n"
                f"Rain yesterday: {result.daily.rain_mm:.1f} mm"
            )

        # Normalise to list — handles both legacy string and new list value
        if isinstance(targets, str):
            targets = [targets] if targets else []

        for target in targets:
            parts = target.split(".", 1)
            if len(parts) != 2:
                _LOGGER.warning(
                    "Zone '%s': invalid notify target '%s' — expected 'domain.service'",
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
            except Exception as err:
                _LOGGER.warning(
                    "Zone '%s': notification to '%s' failed — %s",
                    self.entry.title,
                    target,
                    err,
                )

    # ── Button actions ─────────────────────────────────────────────────────────

    async def async_record_irrigation(self) -> None:
        """Called by the automation blueprint after the valve closes."""
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        if self._last_result is None or not self._last_result.should_water:
            _LOGGER.warning(
                "Zone '%s': record_irrigation called with no pending need — ignored",
                self.entry.title,
            )
            return

        self._bucket.add_irrigation(self._last_result.water_mm)
        await self._store.async_save_bucket(self.entry.entry_id, self._bucket)
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

        _LOGGER.info(
            "Zone '%s': recorded %.1f mm — bucket now %.1f mm (%.0f%%)",
            self.entry.title,
            self._last_result.water_mm,
            self._bucket.level,
            self._bucket.percentage,
        )

    async def async_reset_bucket(self) -> None:
        """Reset bucket to full capacity (manual watering or heavy rain)."""
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        self._bucket.reset()
        await self._store.async_save_bucket(self.entry.entry_id, self._bucket)
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
        _LOGGER.info(
            "Zone '%s': bucket manually reset to %.1f mm",
            self.entry.title,
            self._bucket.level,
        )
