"""Config + options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_ADMIN_ONLY_DOMAINS,
    CONF_ALLOW_ANONYMOUS,
    CONF_ALLOWED_DOMAINS,
    CONF_ALLOWED_USERS,
    CONF_DEFAULT_EXPIRY_HOURS,
    CONF_NOTIFY_SERVICE,
    DEFAULT_ALLOWED_DOMAINS,
    DEFAULT_EXPIRY_HOURS,
    DEFAULT_NOTIFY_SERVICE,
    DOMAIN,
    MAX_EXPIRY_HOURS,
)

# Offered in the options list on top of the defaults. Locks etc. are opt-in.
_OPTIONAL_DOMAINS = [
    "lock",
    "alarm_control_panel",
    "camera",
    "water_heater",
    "lawn_mower",
    "siren",
    "button",
    "input_button",
    "automation",
]


async def _user_options(hass: HomeAssistant) -> list[selector.SelectOptionDict]:
    users = await hass.auth.async_get_users()
    return [
        selector.SelectOptionDict(
            value=u.id, label=f"{u.name}{' (admin)' if u.is_admin else ''}"
        )
        for u in users
        if u.is_active and not u.system_generated
    ]


def _schema(defaults: dict[str, Any], users: list[selector.SelectOptionDict]) -> vol.Schema:
    domains = sorted(set(DEFAULT_ALLOWED_DOMAINS + _OPTIONAL_DOMAINS))
    return vol.Schema(
        {
            vol.Required(
                CONF_ALLOWED_DOMAINS,
                default=defaults.get(CONF_ALLOWED_DOMAINS, DEFAULT_ALLOWED_DOMAINS),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=domains, multiple=True, custom_value=True)
            ),
            vol.Required(
                CONF_ADMIN_ONLY_DOMAINS, default=defaults.get(CONF_ADMIN_ONLY_DOMAINS, [])
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=domains, multiple=True, custom_value=True)
            ),
            vol.Required(
                CONF_ALLOWED_USERS, default=defaults.get(CONF_ALLOWED_USERS, [])
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=users, multiple=True)
            ),
            vol.Required(
                CONF_ALLOW_ANONYMOUS, default=defaults.get(CONF_ALLOW_ANONYMOUS, True)
            ): bool,
            vol.Required(
                CONF_DEFAULT_EXPIRY_HOURS,
                default=defaults.get(CONF_DEFAULT_EXPIRY_HOURS, DEFAULT_EXPIRY_HOURS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=MAX_EXPIRY_HOURS, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_NOTIFY_SERVICE,
                default=defaults.get(CONF_NOTIFY_SERVICE, DEFAULT_NOTIFY_SERVICE),
            ): str,
        }
    )


class OneShotConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="One Shot", data={}, options=user_input)
        return self.async_show_form(
            step_id="user", data_schema=_schema({}, await _user_options(self.hass))
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OneShotOptionsFlow()


class OneShotOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(dict(self.config_entry.options), await _user_options(self.hass)),
        )
