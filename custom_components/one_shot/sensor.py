"""Sensor showing how many One Shots are active."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATED
from .manager import WatchManager


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([OneShotActiveSensor(hass.data[DOMAIN][entry.entry_id], entry)])


class OneShotActiveSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Active"
    _attr_icon = "mdi:timer-sand"
    _attr_should_poll = False
    _attr_native_unit_of_measurement = "One Shots"

    def __init__(self, manager: WatchManager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_active"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self.async_write_ha_state)
        )

    @property
    def native_value(self) -> int:
        return len(self._manager.watches) + len(self._manager.runs)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "one_shots": [
                {"summary": w["summary"], "expires": w["expires"]}
                for w in self._manager.watches.values()
            ],
            "running": [
                {"summary": r["summary"], "resumes_at": r["resume_at"]}
                for r in self._manager.runs.values()
            ],
            "last_result": self._manager.history[0] if self._manager.history else None,
        }
