"""
conftest.py — shared pytest fixtures and Home Assistant stubs.

Why this file exists
--------------------
The integration imports from ``homeassistant.*`` (config entries, storage,
dispatcher, etc). These packages aren't installed in our test environment —
only real HA instances have them.

We inject lightweight stub modules into ``sys.modules`` here, before pytest
collects any test file. This lets every test import ``garden_irrigation.*``
without errors, while keeping the test environment fast and dependency-free.

The stubs only need to satisfy the *import* and *isinstance* checks that
appear in the modules under test. They don't implement real HA behaviour —
that's deliberate: the logic under test (ET₀, Kc, bucket, irrigator) has no
HA dependency; the HA glue code (__init__.py, sensor.py, button.py) is
tested manually by running in a real HA dev environment.
"""

from __future__ import annotations

import sys
from pathlib import Path
import types
from typing import Callable
from unittest.mock import MagicMock

repo_root = Path(__file__).parent.parent
if str(repo_root.joinpath("custom_components")) not in sys.path:
    sys.path.insert(0, str(repo_root.joinpath("custom_components")))


def _inject_ha_stubs() -> None:
    """Register minimal homeassistant stub modules in sys.modules."""

    # ── homeassistant.helpers.storage ──────────────────────────────────────────
    class _Store:
        def __init__(self, hass, version, key): ...
        async def async_load(self): return None
        async def async_save(self, data): ...
        async def async_remove(self): ...

    # ── homeassistant.config_entries ───────────────────────────────────────────
    class _ConfigEntry:
        entry_id = "test_entry"
        title    = "Test Zone"
        data     = {}
        def add_update_listener(self, cb): return lambda: None
        def async_on_unload(self, cb): ...

    # ── homeassistant.core ─────────────────────────────────────────────────────
    class _HomeAssistant:
        config = MagicMock(latitude=45.47, longitude=9.18)
        data   = {}
        services = MagicMock()
        def async_create_task(self, coro, name=None): ...

    def _callback(func):
        return func

    # ── homeassistant.helpers.dispatcher ──────────────────────────────────────
    def _async_dispatcher_send(hass, signal, *args): ...

    # ── homeassistant.helpers.event ────────────────────────────────────────────
    def _async_track_time_change(hass, action, **kwargs):
        return lambda: None   # returns a cancel callback

    # ── homeassistant.helpers.aiohttp_client ───────────────────────────────────
    def _async_get_clientsession(hass):
        return MagicMock()

    # ── Assemble stub modules ──────────────────────────────────────────────────
    mods: dict[str, types.ModuleType] = {}

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        mods[name] = m
        return m

    ha            = _mod("homeassistant")
    ha_core       = _mod("homeassistant.core")
    ha_helpers    = _mod("homeassistant.helpers")
    ha_storage    = _mod("homeassistant.helpers.storage")
    ha_dispatcher = _mod("homeassistant.helpers.dispatcher")
    ha_event      = _mod("homeassistant.helpers.event")
    ha_aiohttp    = _mod("homeassistant.helpers.aiohttp_client")
    ha_ce         = _mod("homeassistant.config_entries")

    ha_core.HomeAssistant        = _HomeAssistant
    ha_core.callback             = _callback
    ha_core.CALLBACK_TYPE        = Callable[[], None]
    ha_storage.Store             = _Store
    ha_dispatcher.async_dispatcher_send = _async_dispatcher_send
    ha_event.async_track_time_change    = _async_track_time_change
    ha_aiohttp.async_get_clientsession  = _async_get_clientsession
    ha_ce.ConfigEntry            = _ConfigEntry

    for name, mod in mods.items():
        sys.modules.setdefault(name, mod)


_inject_ha_stubs()