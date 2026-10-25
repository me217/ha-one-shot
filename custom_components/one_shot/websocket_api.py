"""Websocket commands used by the One Shot sidebar panel."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .manager import WatchError, WatchManager


def _manager(hass: HomeAssistant) -> WatchManager | None:
    managers = hass.data.get(DOMAIN, {})
    return next((m for m in managers.values() if isinstance(m, WatchManager)), None)


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_cancel)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "One Shot is not loaded")
        return
    connection.send_result(msg["id"], await manager.panel_data())


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/cancel", vol.Required("one_shot_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_cancel(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "One Shot is not loaded")
        return
    try:
        result = await manager.async_cancel(msg["one_shot_id"], connection.user.id)
    except WatchError as err:
        connection.send_error(msg["id"], "cancel_failed", str(err))
        return
    connection.send_result(msg["id"], result)
