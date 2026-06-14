"""
Config flow — Garden Irrigation setup wizard.

Two-step flow (config + options):
  Step 1 "user"        : zone name, calculation time
  Step 2 "zone_params" : crops, growth stage slider, system parameters

Growth stage input
------------------
Instead of asking for a planting date (which requires the user to remember
an exact date), the form asks two questions:
  - Which growth stage are you in? (Initial / Development / Mid-season / Late)
  - How far through that stage? (0 - 100 % slider)

From those inputs and the average stage durations of the selected crops,
we compute a planting_date and store that. Everything downstream (kc.py,
irrigator.py, sensors) is unchanged — they still work with planting_date.

Options flow bonus
------------------
When the user re-opens the config, we reverse the computation:
  (today - planting_date).days → current stage + progress %
So the form pre-fills with the actual current growth state, not whatever
was entered at setup time.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CALCULATION_TIME,
    CONF_CROPS,
    CONF_FLOW_RATE,
    CONF_LOW_THRESHOLD,
    CONF_MAX_BUCKET,
    CONF_PLANTING_DATE,
    CONF_ZONE_AREA,
    CONF_ZONE_NAME,
    DEFAULT_CALCULATION_TIME,
    DEFAULT_LOW_THRESHOLD,
    DEFAULT_MAX_BUCKET,
    DOMAIN,
)
from .kc import available_crops, load_crops

# ── Growth stage constants ─────────────────────────────────────────────────────
# These are UI-only keys. They are consumed in _stage_to_planting_date() and
# never written to the config entry.

_STAGE_INITIAL = "initial"
_STAGE_DEVELOPMENT = "development"
_STAGE_MID = "mid"
_STAGE_LATE = "late"

_CONF_GROWTH_STAGE = "growth_stage"
_CONF_STAGE_PROGRESS = "stage_progress"

_STAGE_OPTIONS = [
    selector.SelectOptionDict(
        value=_STAGE_INITIAL,
        label="🌱 Initial — seedling, sparse cover",
    ),
    selector.SelectOptionDict(
        value=_STAGE_DEVELOPMENT,
        label="🌿 Development — growing canopy, flowering",
    ),
    selector.SelectOptionDict(
        value=_STAGE_MID,
        label="🍅 Mid-season — full cover, peak demand",
    ),
    selector.SelectOptionDict(
        value=_STAGE_LATE,
        label="🍂 Late season — ripening, senescence",
    ),
]


# ── Stage <-> planting date helpers ─────────────────────────────────────────────


def _avg_stage_durations(crop_ids: list[str]) -> tuple[int, int, int, int]:
    """
    Return (l_ini, l_dev, l_mid, l_late) averaged across the selected crops.

    Falls back to 30 days per stage when no crops are recognised.
    """
    registry = load_crops()
    crops = [registry[cid] for cid in crop_ids if cid in registry]
    if not crops:
        return 30, 30, 30, 30
    return (
        round(sum(c.l_ini for c in crops) / len(crops)),
        round(sum(c.l_dev for c in crops) / len(crops)),
        round(sum(c.l_mid for c in crops) / len(crops)),
        round(sum(c.l_late for c in crops) / len(crops)),
    )


def _stage_to_planting_date(
    stage: str,
    progress_pct: int,
    crop_ids: list[str],
    today: date,
) -> date:
    """
    Convert (stage, progress %) → planting_date.

    Example:
        stage="mid", progress_pct=25, crops=["tomato"]
        Tomato: l_ini=30, l_dev=40, l_mid=40
        days_elapsed = 30 + 40 + round(0.25 * 40) = 80
        planting_date = today - 80 days
    """
    l_ini, l_dev, l_mid, l_late = _avg_stage_durations(crop_ids)

    stage_start = {
        _STAGE_INITIAL: 0,
        _STAGE_DEVELOPMENT: l_ini,
        _STAGE_MID: l_ini + l_dev,
        _STAGE_LATE: l_ini + l_dev + l_mid,
    }.get(stage, 0)

    stage_len = {
        _STAGE_INITIAL: l_ini,
        _STAGE_DEVELOPMENT: l_dev,
        _STAGE_MID: l_mid,
        _STAGE_LATE: l_late,
    }.get(stage, 30)

    days_elapsed = stage_start + round(progress_pct / 100 * stage_len)
    return today - timedelta(days=days_elapsed)


def _planting_date_to_stage(
    planting_date: date,
    crop_ids: list[str],
    today: date,
) -> tuple[str, int]:
    """
    Convert planting_date → (stage, progress %).

    Used to pre-fill the options form with the current growth state.
    Returns progress clamped to [0, 100].
    """
    l_ini, l_dev, l_mid, l_late = _avg_stage_durations(crop_ids)
    days = max(0, (today - planting_date).days)

    if days < l_ini:
        return _STAGE_INITIAL, _pct(days, l_ini)

    days -= l_ini
    if days < l_dev:
        return _STAGE_DEVELOPMENT, _pct(days, l_dev)

    days -= l_dev
    if days < l_mid:
        return _STAGE_MID, _pct(days, l_mid)

    days -= l_mid
    return _STAGE_LATE, min(100, _pct(days, l_late))


def _pct(part: int, total: int) -> int:
    """Integer percentage, safe against zero division."""
    return round(part / total * 100) if total else 0


# ── Schema builders ────────────────────────────────────────────────────────────


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
                CONF_CALCULATION_TIME,
                default=d.get(CONF_CALCULATION_TIME, DEFAULT_CALCULATION_TIME),
            ): selector.TimeSelector(),
        }
    )


def _step2_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """
    Schema for step 2: crops, growth stage, system parameters.

    ``defaults`` may contain either:
    - CONF_PLANTING_DATE + CONF_CROPS  (options flow: reverse-computed from stored date)
    - _CONF_GROWTH_STAGE + _CONF_STAGE_PROGRESS  (re-showing after a validation error)
    """
    d = defaults or {}

    crop_options = [
        selector.SelectOptionDict(value=c.id, label=c.name) for c in available_crops()
    ]

    return vol.Schema(
        {
            # ── Crops ──────────────────────────────────────────────────────────
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
            # ── Growth stage ───────────────────────────────────────────────────
            vol.Required(
                _CONF_GROWTH_STAGE,
                default=d.get(_CONF_GROWTH_STAGE, _STAGE_MID),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_STAGE_OPTIONS,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                _CONF_STAGE_PROGRESS,
                default=d.get(_CONF_STAGE_PROGRESS, 0),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=100,
                    step=5,
                    unit_of_measurement="%",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            # ── System parameters ──────────────────────────────────────────────
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
    """Strip seconds from TimeSelector output: "HH:MM:SS" → "HH:MM"."""
    return value[:5]


def _process_step2(user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """
    Validate step 2 input and compute planting_date from stage + progress.

    Returns (processed_data, errors).
    ``processed_data`` has CONF_PLANTING_DATE set and the raw stage/progress
    keys removed — it is ready to merge into the config entry.
    """
    errors: dict[str, str] = {}
    data = dict(user_input)

    if not data.get(CONF_CROPS):
        errors[CONF_CROPS] = "no_crops_selected"

    if float(data.get(CONF_LOW_THRESHOLD, 0)) >= float(data.get(CONF_MAX_BUCKET, 1)):
        errors[CONF_LOW_THRESHOLD] = "threshold_above_max"

    if errors:
        return data, errors

    # Convert stage + progress → planting_date, then remove the UI-only keys
    stage = data.pop(_CONF_GROWTH_STAGE)
    progress = int(data.pop(_CONF_STAGE_PROGRESS))
    data[CONF_PLANTING_DATE] = _stage_to_planting_date(
        stage=stage,
        progress_pct=progress,
        crop_ids=data[CONF_CROPS],
        today=date.today(),
    ).isoformat()

    return data, {}


def _step2_defaults_from_current(current: dict[str, Any]) -> dict[str, Any]:
    """
    Build step 2 form defaults from an existing config entry.

    Reverse-computes growth_stage + stage_progress from the stored
    planting_date so the options form shows the *current* growth state.
    """
    defaults = dict(current)

    planting_str = current.get(CONF_PLANTING_DATE, "")
    crop_ids = current.get(CONF_CROPS, [])

    if planting_str:
        try:
            stage, progress = _planting_date_to_stage(
                planting_date=date.fromisoformat(planting_str),
                crop_ids=crop_ids,
                today=date.today(),
            )
            defaults[_CONF_GROWTH_STAGE] = stage
            defaults[_CONF_STAGE_PROGRESS] = progress
        except (ValueError, KeyError):
            defaults[_CONF_GROWTH_STAGE] = _STAGE_MID
            defaults[_CONF_STAGE_PROGRESS] = 0

    return defaults


# ── Config flow ────────────────────────────────────────────────────────────────


class GardenIrrigationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial "Add Integration" setup wizard."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_CALCULATION_TIME] = _normalise_time(
                user_input[CONF_CALCULATION_TIME]
            )
            self._data.update(user_input)
            return await self.async_step_zone_params()

        return self.async_show_form(
            step_id="user",
            data_schema=_step1_schema(),
            errors=errors,
        )

    async def async_step_zone_params(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            processed, errors = _process_step2(user_input)
            if not errors:
                self._data.update(processed)
                return self.async_create_entry(
                    title=self._data[CONF_ZONE_NAME],
                    data=self._data,
                )
            # Re-show form with user's raw input as defaults so nothing is lost
            return self.async_show_form(
                step_id="zone_params",
                data_schema=_step2_schema(user_input),
                errors=errors,
            )

        return self.async_show_form(
            step_id="zone_params",
            data_schema=_step2_schema(),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> GardenIrrigationOptionsFlow:
        return GardenIrrigationOptionsFlow(config_entry)


# ── Options flow ───────────────────────────────────────────────────────────────


class GardenIrrigationOptionsFlow(OptionsFlow):
    """
    Handle the "Configure" button on an existing zone entry.

    Step 2 is pre-filled with the current growth state (computed live from
    the stored planting_date), not the state at the time of original setup.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._data: dict[str, Any] = {}

    def _current(self) -> dict[str, Any]:
        return {**self._config_entry.data, **self._config_entry.options}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_user(user_input)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_CALCULATION_TIME] = _normalise_time(
                user_input[CONF_CALCULATION_TIME]
            )
            self._data.update(user_input)
            return await self.async_step_zone_params()

        return self.async_show_form(
            step_id="user",
            data_schema=_step1_schema(self._current()),
            errors=errors,
        )

    async def async_step_zone_params(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            processed, errors = _process_step2(user_input)
            if not errors:
                self._data.update(processed)
                return self.async_create_entry(title="", data=self._data)
            return self.async_show_form(
                step_id="zone_params",
                data_schema=_step2_schema(user_input),
                errors=errors,
            )

        # Pre-fill: reverse-compute current stage + progress from stored planting_date
        return self.async_show_form(
            step_id="zone_params",
            data_schema=_step2_schema(_step2_defaults_from_current(self._current())),
            errors=errors,
        )
