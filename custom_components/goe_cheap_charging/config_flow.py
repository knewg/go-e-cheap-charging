"""Config flow for GO-e Cheap Charging."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_BREAKER_LIMIT,
    CONF_CHARGER_N_PHASES,
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
    DEFAULT_CHARGER_N_PHASES,
    DEFAULT_CHARGER_PHASE,
    DEFAULT_EFFICIENCY,
    DEFAULT_MAX_AMP,
    DEFAULT_MIN_AMP,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_BOX_INT = NumberSelectorConfig(mode=NumberSelectorMode.BOX)


def _goe_serials(hass: HomeAssistant) -> list[str]:
    """Return go-e charger serial numbers discovered via entity IDs like *go_echarger_SERIAL_*."""
    reg = er.async_get(hass)
    serials: set[str] = set()
    for entry in reg.entities.values():
        m = re.search(r"go_echarger_(\w+?)_", entry.entity_id)
        if m:
            serials.add(m.group(1))

    return sorted(serials)


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


class ChargingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GO-e Cheap Charging."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._reconfigure: bool = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: charger serial + battery/charger parameters."""
        if user_input is not None:
            user_input[CONF_EFFICIENCY] = user_input[CONF_EFFICIENCY] / 100.0
            user_input[CONF_CHARGER_N_PHASES] = int(user_input[CONF_CHARGER_N_PHASES])
            self._data.update(user_input)
            if user_input[CONF_CHARGER_N_PHASES] == 1:
                return await self.async_step_charger_phase()
            self._data[CONF_CHARGER_PHASE] = DEFAULT_CHARGER_PHASE
            return await self.async_step_electrical()

        cur = self._data
        # When re-entering (reconfigure), convert stored fraction back to percent for display
        default_efficiency = int(round(cur.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY / 100.0) * 100)) if CONF_EFFICIENCY in cur else DEFAULT_EFFICIENCY

        goe_serials = _goe_serials(self.hass)
        schema = vol.Schema(
            {
                vol.Required(CONF_CHARGER_SERIAL, default=cur.get(CONF_CHARGER_SERIAL, "")): SelectSelector(SelectSelectorConfig(options=goe_serials, mode=SelectSelectorMode.DROPDOWN)) if goe_serials else TextSelector(),
                vol.Required(CONF_BATTERY_CAPACITY, default=cur.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)): NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=10, max=200, step=0.5, unit_of_measurement="kWh")),
                vol.Required(CONF_EFFICIENCY, default=default_efficiency): NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=50, max=100, step=1, unit_of_measurement="%")),
                vol.Required(CONF_BREAKER_LIMIT, default=cur.get(CONF_BREAKER_LIMIT, DEFAULT_BREAKER_LIMIT)): NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=10, max=63, step=1, unit_of_measurement="A")),
                vol.Required(CONF_CHARGER_N_PHASES, default=str(cur.get(CONF_CHARGER_N_PHASES, DEFAULT_CHARGER_N_PHASES))): SelectSelector(SelectSelectorConfig(options=["1", "3"], mode=SelectSelectorMode.DROPDOWN)),
                vol.Required(CONF_MIN_AMP, default=cur.get(CONF_MIN_AMP, DEFAULT_MIN_AMP)): NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=6, max=32, step=1, unit_of_measurement="A")),
                vol.Required(CONF_MAX_AMP, default=cur.get(CONF_MAX_AMP, DEFAULT_MAX_AMP)): NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=6, max=32, step=1, unit_of_measurement="A")),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_charger_phase(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1b (single-phase only): which phase is the charger on?"""
        if user_input is not None:
            user_input[CONF_CHARGER_PHASE] = int(user_input[CONF_CHARGER_PHASE])
            self._data.update(user_input)
            return await self.async_step_electrical()

        cur = self._data
        schema = vol.Schema(
            {
                vol.Required(CONF_CHARGER_PHASE, default=cur.get(CONF_CHARGER_PHASE, DEFAULT_CHARGER_PHASE)): SelectSelector(SelectSelectorConfig(options=["1", "2", "3"], mode=SelectSelectorMode.DROPDOWN)),
            }
        )
        return self.async_show_form(step_id="charger_phase", data_schema=schema)

    async def async_step_electrical(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: phase current sensors."""
        amp_sensors = _amp_sensor_entities(self.hass)

        if user_input is not None:
            self._data.update(user_input)
            if self._reconfigure:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(), data=self._data
                )
            return self.async_create_entry(title="GO-e Cheap Charging", data=self._data)

        cur = self._data
        schema = vol.Schema(
            {
                vol.Required(CONF_PHASE_L1_ENTITY, default=cur.get(CONF_PHASE_L1_ENTITY)): vol.In(amp_sensors) if amp_sensors else str,
                vol.Required(CONF_PHASE_L2_ENTITY, default=cur.get(CONF_PHASE_L2_ENTITY)): vol.In(amp_sensors) if amp_sensors else str,
                vol.Required(CONF_PHASE_L3_ENTITY, default=cur.get(CONF_PHASE_L3_ENTITY)): vol.In(amp_sensors) if amp_sensors else str,
            }
        )

        return self.async_show_form(step_id="electrical", data_schema=schema)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reconfigure: start at step 1 with current values pre-filled."""
        self._reconfigure = True
        self._data = dict(self._get_reconfigure_entry().data)
        return await self.async_step_user(user_input)
