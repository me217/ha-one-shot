"""One Shot: Assist proposes, the user confirms, plain code does the rest."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from . import websocket_api as one_shot_ws
from .const import DOMAIN, PANEL_STATIC_URL, PANEL_URL_PATH, VERSION
from .llm_api import OneShotAPI
from .manager import WatchManager

PLATFORMS = ["sensor"]
_STATIC_FLAG = f"{DOMAIN}_static_registered"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = WatchManager(hass, entry)
    await manager.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    entry.async_on_unload(llm.async_register_api(hass, OneShotAPI(hass, manager)))
    entry.async_on_unload(manager.async_shutdown)
    entry.async_on_unload(entry.add_update_listener(_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_setup_panel(hass)
    return True


async def _async_setup_panel(hass: HomeAssistant) -> None:
    if not hass.data.get(_STATIC_FLAG):
        one_shot_ws.async_register(hass)
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    PANEL_STATIC_URL,
                    str(Path(__file__).parent / "frontend"),
                    cache_headers=False,
                )
            ]
        )
        hass.data[_STATIC_FLAG] = True

    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="one-shot-panel",
        frontend_url_path=PANEL_URL_PATH,
        module_url=f"{PANEL_STATIC_URL}/one-shot-panel.js?v={VERSION}",
        sidebar_title="One Shot",
        sidebar_icon="mdi:bullseye-arrow",
        require_admin=True,
        config={},
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
    return unloaded


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
