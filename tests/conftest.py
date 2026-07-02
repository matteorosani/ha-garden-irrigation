"""
Shared pytest configuration for Garden Irrigation tests.

Path setup
----------
Adds custom_components/ to sys.path so all test files can write:

    from garden_irrigation.et0 import et0_for_date

without any per-file sys.path manipulation.

Home Assistant stubs
--------------------
When homeassistant is NOT installed (running tests outside the dev container),
we inject minimal stubs so HA-adjacent modules can be imported.

When homeassistant IS installed (inside the dev container), the real package
is used and stubs are skipped. Thanks to the lazy-import pattern in
__init__.py, importing garden_irrigation submodules no longer triggers
HA's deep import chain, so unit tests run cleanly either way.

Type-checker note
-----------------
Stub attributes are set via setattr() rather than direct assignment
(e.g. ``module.Foo = Bar``) because ModuleType doesn't declare arbitrary
attributes and type-checkers (pyright/pylance) would flag those as errors.
setattr() is equivalent at runtime and silences the warnings.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# ── 1. Add custom_components/ to sys.path ─────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
CUSTOM_COMPONENTS = REPO_ROOT / "custom_components"

if str(CUSTOM_COMPONENTS) not in sys.path:
    sys.path.insert(0, str(CUSTOM_COMPONENTS))


# ── 2. HA stubs (only when homeassistant is not installed) ────────────────────


def _ha_is_available() -> bool:
    try:
        import homeassistant  # noqa: F401

        return True
    except ImportError:
        return False


def _inject_ha_stubs() -> None:
    """
    Register minimal homeassistant stub modules in sys.modules.

    Uses setattr(module, name, value) throughout to avoid type-checker
    complaints about assigning unknown attributes to ModuleType instances.
    """

    # ── Stub classes ──────────────────────────────────────────────────────────

    class _Store:
        def __init__(self, hass: object, version: int, key: str) -> None: ...
        async def async_load(self) -> object:
            return None

        async def async_save(self, data: object) -> None: ...
        async def async_remove(self) -> None: ...

    class _ConfigEntry:
        entry_id: str = "test_entry"
        title: str = "Test Zone"
        data: dict = {}  # noqa: RUF012
        options: dict = {}  # noqa: RUF012

        def add_update_listener(self, cb: object) -> object:
            return lambda: None

        def async_on_unload(self, cb: object) -> None: ...

    class _ConfigFlow:
        VERSION = 1

        def __init_subclass__(cls, domain: object = None, **kw: object) -> None:
            super().__init_subclass__(**kw)

    class _OptionsFlow: ...

    class _HomeAssistant:
        config = MagicMock(latitude=45.47, longitude=9.18, config_dir="/config")
        data: dict = {}  # noqa: RUF012
        services = MagicMock()

        def async_create_task(self, coro: object, name: object = None) -> None: ...

    class _SensorEntity:
        _attr_has_entity_name: bool = False
        _attr_should_poll: bool = True

    class _ButtonEntity: ...

    class _Stub:
        def __init__(self, *a: object, **kw: object) -> None: ...
        def __call__(self, *a: object, **kw: object) -> _Stub:
            return self

        def __getattr__(self, name: str) -> _Stub:
            return _Stub()

    def _callback(func: object) -> object:
        return func

    def _async_dispatcher_send(hass: object, signal: object, *args: object) -> None: ...
    def _async_track_time_change(hass: object, action: object, **kw: object) -> object:
        return lambda: None

    def _async_get_clientsession(hass: object) -> MagicMock:
        return MagicMock()

    # ── Build stub modules ────────────────────────────────────────────────────

    def _mod(name: str) -> types.ModuleType:
        """Get-or-create a stub module in sys.modules."""
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
        return sys.modules[name]

    ha_core = _mod("homeassistant.core")
    ha_helpers = _mod("homeassistant.helpers")
    ha_storage = _mod("homeassistant.helpers.storage")
    ha_dispatcher = _mod("homeassistant.helpers.dispatcher")
    ha_event = _mod("homeassistant.helpers.event")
    ha_aiohttp = _mod("homeassistant.helpers.aiohttp_client")
    ha_selector = _mod("homeassistant.helpers.selector")
    ha_ce = _mod("homeassistant.config_entries")
    ha_entity = _mod("homeassistant.helpers.entity")
    ha_ep = _mod("homeassistant.helpers.entity_platform")
    ha_sensor = _mod("homeassistant.components.sensor")
    ha_button = _mod("homeassistant.components.button")
    _mod("homeassistant")
    _mod("homeassistant.components")

    # ── Assign via setattr (avoids type-checker ModuleType warnings) ──────────

    _stub = _Stub()

    ha_core.HomeAssistant = _HomeAssistant
    ha_core.callback = _callback
    ha_storage.Store = _Store
    ha_dispatcher.async_dispatcher_send = _async_dispatcher_send
    ha_dispatcher.async_dispatcher_connect = MagicMock(return_value=lambda: None)
    ha_event.async_track_time_change = _async_track_time_change
    ha_aiohttp.async_get_clientsession = _async_get_clientsession
    ha_ce.ConfigEntry = _ConfigEntry
    ha_ce.ConfigFlow = _ConfigFlow
    ha_ce.OptionsFlow = _OptionsFlow
    ha_entity.DeviceInfo = dict
    ha_ep.AddEntitiesCallback = MagicMock

    class _RestoreSensor(_SensorEntity):
        async def async_get_last_sensor_data(self):
            return None

        async def async_added_to_hass(self):
            pass

    ha_sensor.SensorEntity = _SensorEntity
    ha_sensor.RestoreSensor = _RestoreSensor
    ha_sensor.SensorExtraStoredData = MagicMock
    ha_sensor.SensorDeviceClass = _stub
    ha_sensor.SensorStateClass = _stub
    ha_button.ButtonEntity = _ButtonEntity
    ha_core.callback = _callback

    ha_selector.SelectOptionDict = dict
    ha_selector.SelectSelectorMode = _stub
    ha_selector.NumberSelectorMode = _stub
    ha_selector.TextSelectorType = _stub
    for _cls_name in [
        "TextSelector",
        "TextSelectorConfig",
        "TimeSelector",
        "NumberSelector",
        "NumberSelectorConfig",
        "SelectSelector",
        "SelectSelectorConfig",
        "EntitySelector",
        "EntitySelectorConfig",
    ]:
        setattr(ha_selector, _cls_name, _Stub)

    # Make  `from homeassistant.helpers import selector`  work
    ha_helpers.selector = ha_selector


if not _ha_is_available():
    _inject_ha_stubs()
