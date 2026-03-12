"""Config flow for EV Smart Charging."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_BREAKER_LIMIT,
    CONF_CAR_DEVICE_ID,
    CONF_CAR_SOC_ENTITY,
    CONF_CHARGER_PHASE,
    CONF_CHARGER_SERIAL,
    CONF_EFFICIENCY,
    CONF_MAX_AMP,
    CONF_MIN_AMP,
    CONF_PHASE_L1_ENTITY,
    CONF_PHASE_L2_ENTITY,
    CONF_PHASE_L3_ENTITY,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_BREAKER_LIMIT,
    DEFAULT_CHARGER_PHASE,
    DEFAULT_EFFICIENCY,
    DEFAULT_MAX_AMP,
    DEFAULT_MIN_AMP,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _amp_sensor_entities(hass: HomeAssistant) -> list[str]:
    """Return sensor entities whose unit of measurement is A (amps)."""
    reg = er.async_get(hass)
    result = []
    for entry in reg.entities.values():
        if entry.domain != "sensor":
            continue
        state = hass.states.get(entry.entity_id)
        if state and state.attributes.get("unit_of_measurement") == "A":
            result.append(entry.entity_id)
    return sorted(result)


def _kia_soc_entities(hass: HomeAssistant) -> list[str]:
    """Return sensor entities from the kia_uvo integration."""
    reg = er.async_get(hass)
    result = []
    for entry in reg.entities.values():
        if entry.domain != "sensor":
            continue
        if entry.platform and "kia" in entry.platform.lower():
            result.append(entry.entity_id)
    return sorted(result)


def _kia_device_ids(hass: HomeAssistant) -> list[str]:
    """Return device IDs associated with kia_uvo entities."""
    from homeassistant.helpers import device_registry as dr
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    ids = set()
    for entry in ent_reg.entities.values():
        if entry.platform and "kia" in entry.platform.lower() and entry.device_id:
            ids.add(entry.device_id)
    return sorted(ids)


class EvSmartChargingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EV Smart Charging."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: charger serial + car entities."""
        errors: dict[str, str] = {}

        amp_sensors = _amp_sensor_entities(self.hass)
        kia_sensors = _kia_soc_entities(self.hass)
        kia_devices = _kia_device_ids(self.hass)

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_electrical()

        schema = vol.Schema(
            {
                vol.Required(CONF_CHARGER_SERIAL): str,
                vol.Required(CONF_CAR_SOC_ENTITY): vol.In(kia_sensors) if kia_sensors else str,
                vol.Required(CONF_CAR_DEVICE_ID): vol.In(kia_devices) if kia_devices else str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "kia_hint": "Select the Kia UVO battery level sensor",
            },
        )

    async def async_step_electrical(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: phase current sensors."""
        amp_sensors = _amp_sensor_entities(self.hass)

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_charger_params()

        schema = vol.Schema(
            {
                vol.Required(CONF_PHASE_L1_ENTITY): vol.In(amp_sensors) if amp_sensors else str,
                vol.Required(CONF_PHASE_L2_ENTITY): vol.In(amp_sensors) if amp_sensors else str,
                vol.Required(CONF_PHASE_L3_ENTITY): vol.In(amp_sensors) if amp_sensors else str,
            }
        )

        return self.async_show_form(step_id="electrical", data_schema=schema)

    async def async_step_charger_params(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: battery and charger parameters."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="EV Smart Charging", data=self._data)

        schema = vol.Schema(
            {
                vol.Required(CONF_BATTERY_CAPACITY, default=DEFAULT_BATTERY_CAPACITY): vol.Coerce(float),
                vol.Required(CONF_EFFICIENCY, default=DEFAULT_EFFICIENCY): vol.All(
                    vol.Coerce(float), vol.Range(min=0.5, max=1.0)
                ),
                vol.Required(CONF_BREAKER_LIMIT, default=DEFAULT_BREAKER_LIMIT): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=63)
                ),
                vol.Required(CONF_CHARGER_PHASE, default=DEFAULT_CHARGER_PHASE): vol.In([1, 2, 3]),
                vol.Required(CONF_MIN_AMP, default=DEFAULT_MIN_AMP): vol.All(
                    vol.Coerce(int), vol.Range(min=6, max=32)
                ),
                vol.Required(CONF_MAX_AMP, default=DEFAULT_MAX_AMP): vol.All(
                    vol.Coerce(int), vol.Range(min=6, max=32)
                ),
            }
        )

        return self.async_show_form(step_id="charger_params", data_schema=schema)
