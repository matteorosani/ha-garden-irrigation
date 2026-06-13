"""
Config flow — Garden Irrigation setup wizard.

Two-step flow (config + options):
  Step 1 "user"        : zone name, valve entity, watering time
  Step 2 "zone_params" : crops, planting date, area, flow rate, bucket settings

Both ConfigFlow (initial setup) and OptionsFlow (editing an existing zone)
use the same two steps and the same schema-building helpers, so the UI
is consistent between first setup and later edits.

Data stored in config entry
---------------------------
All values go into entry.data on initial setup.
The OptionsFlow stores its output in entry.options, which __init__.py
merges on top of entry.data with {**entry.data, **entry.options}.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CROPS,
    CONF_FLOW_RATE,
    CONF_LOW_THRESHOLD,
    CONF_MAX_BUCKET,
    CONF_PLANTING_DATE,
    CONF_WATERING_TIME,
    CONF_ZONE_AREA,
    CONF_ZONE_NAME,
    DEFAULT_LOW_THRESHOLD,
    DEFAULT_MAX_BUCKET,
    DEFAULT_WATERING_TIME,
    DOMAIN,
)
from .kc import available_crops


# ── Schema builders ────────────────────────────────────────────────────────────
# Separated from the flow classes so both ConfigFlow and OptionsFlow can
# call them with optional defaults (for pre-filling the options form).

def _step1_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Schema for step 1: zone identity."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_ZONE_NAME,
                default=d.get(CONF_ZONE_NAME, ""),
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            ),
            vol.Required(
                CONF_WATERING_TIME,
                default=d.get(CONF_WATERING_TIME, DEFAULT_WATERING_TIME),
            ): selector.TimeSelector(),
        }
    )


def _step2_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Schema for step 2: crops, planting date, and system parameters."""
    d = defaults or {}

    # Build the crop options list from the bundled crops.json.
    # Done at call time (not module load) so the file is read after HA starts.
    crop_options = [
        selector.SelectOptionDict(value=c.id, label=c.name)
        for c in available_crops()
    ]

    return vol.Schema(
        {
            vol.Required(
                CONF_CROPS,
                default=d.get(CONF_CROPS, []),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=crop_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_PLANTING_DATE,
                default=d.get(CONF_PLANTING_DATE, ""),
            ): selector.DateSelector(),
            vol.Required(
                CONF_ZONE_AREA,
                default=d.get(CONF_ZONE_AREA, 10.0),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=10_000,
                    step=0.1,
                    unit_of_measurement="m²",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_FLOW_RATE,
                default=d.get(CONF_FLOW_RATE, 4.0),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=1_000,
                    step=0.1,
                    unit_of_measurement="L/min",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MAX_BUCKET,
                default=d.get(CONF_MAX_BUCKET, DEFAULT_MAX_BUCKET),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=200,
                    step=1,
                    unit_of_measurement="mm",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_LOW_THRESHOLD,
                default=d.get(CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=200,
                    step=1,
                    unit_of_measurement="mm",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalise_time(value: str) -> str:
    """
    Normalise a time string to "HH:MM".

    HA's TimeSelector returns "HH:MM:SS"; the coordinator only needs "HH:MM".
    Passing an already-normalised "HH:MM" string is also safe.
    """
    return value[:5]


def _validate_step2(data: dict[str, Any]) -> dict[str, str]:
    """
    Cross-field validation for step 2.

    Returns a dict of {field: error_key} (empty dict = no errors).
    """
    errors: dict[str, str] = {}

    if not data.get(CONF_CROPS):
        errors[CONF_CROPS] = "no_crops_selected"

    try:
        date.fromisoformat(data[CONF_PLANTING_DATE])
    except (ValueError, KeyError):
        errors[CONF_PLANTING_DATE] = "invalid_date"

    if float(data.get(CONF_LOW_THRESHOLD, 0)) >= float(data.get(CONF_MAX_BUCKET, 1)):
        errors[CONF_LOW_THRESHOLD] = "threshold_above_max"

    return errors


# ── Config flow ────────────────────────────────────────────────────────────────

class GardenIrrigationConfigFlow(ConfigFlow, domain=DOMAIN):
    """
    Handle the initial "Add Integration" setup wizard.

    State is accumulated in self._data across the two steps and written
    to the config entry at the end of step 2.
    """

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    # ── Step 1: zone name, valve, time ─────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # Normalise time before storing (TimeSelector gives "HH:MM:SS")
            user_input[CONF_WATERING_TIME] = _normalise_time(
                user_input[CONF_WATERING_TIME]
            )
            self._data.update(user_input)
            return await self.async_step_zone_params()

        return self.async_show_form(
            step_id="user",
            data_schema=_step1_schema(),
            errors=errors,
        )

    # ── Step 2: crops, planting date, system parameters ────────────────────────

    async def async_step_zone_params(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_step2(user_input)
            if not errors:
                self._data.update(user_input)
                return self.async_create_entry(
                    title=self._data[CONF_ZONE_NAME],
                    data=self._data,
                )

        return self.async_show_form(
            step_id="zone_params",
            data_schema=_step2_schema(user_input or {}),
            errors=errors,
        )

    # ── Options flow hook ──────────────────────────────────────────────────────

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "GardenIrrigationOptionsFlow":
        return GardenIrrigationOptionsFlow(config_entry)


# ── Options flow ───────────────────────────────────────────────────────────────

class GardenIrrigationOptionsFlow(OptionsFlow):
    """
    Handle the "Configure" button on an existing zone entry.

    Pre-fills every field with the current value so the user only needs
    to change what they want to update.

    Output goes into entry.options. __init__.py merges options on top of
    data with {**entry.data, **entry.options} so that options always win.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._data: dict[str, Any] = {}

    def _current(self) -> dict[str, Any]:
        """Merged current values: options override data."""
        return {**self._config_entry.data, **self._config_entry.options}

    # ── Step 1 ─────────────────────────────────────────────────────────────────

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """OptionsFlow entry point — delegates to step 1."""
        return await self.async_step_user(user_input)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_WATERING_TIME] = _normalise_time(
                user_input[CONF_WATERING_TIME]
            )
            self._data.update(user_input)
            return await self.async_step_zone_params()

        return self.async_show_form(
            step_id="user",
            data_schema=_step1_schema(self._current()),
            errors=errors,
        )

    # ── Step 2 ─────────────────────────────────────────────────────────────────

    async def async_step_zone_params(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_step2(user_input)
            if not errors:
                self._data.update(user_input)
                # async_create_entry in an OptionsFlow writes to entry.options
                return self.async_create_entry(title="", data=self._data)

        return self.async_show_form(
            step_id="zone_params",
            data_schema=_step2_schema(user_input or self._current()),
            errors=errors,
        )