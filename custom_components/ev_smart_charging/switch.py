"""Switch entities for EV Smart Charging."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, WEEKDAYS
from .coordinator import EvSmartChargingCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EvSmartChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [
        EvSmartChargingMasterSwitch(coordinator, entry),
        EvChargingChargeNowSwitch(coordinator, entry),
    ]
    for day in WEEKDAYS:
        entities.append(EvChargingDaySwitch(coordinator, entry, day))
    async_add_entities(entities)


class _BaseSwitch(RestoreEntity, SwitchEntity):
    """Base switch with state restoration."""

    _attr_should_poll = False

    def __init__(self, coordinator: EvSmartChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last:
            await self._async_restore(last.state == "on")
        self._coordinator.schedule_pending_rebuild()

    async def _async_restore(self, value: bool) -> None:
        pass


class EvSmartChargingMasterSwitch(_BaseSwitch):
    """Master smart-charging enable/disable switch."""

    def __init__(self, coordinator: EvSmartChargingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_smart_enabled"
        self._attr_name = "EV Charging Smart Enabled"

    @property
    def is_on(self) -> bool:
        return self._coordinator.get_smart_enabled()

    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.set_smart_enabled(True)
        self.async_write_ha_state()
        await self._coordinator._async_rebuild_schedule()

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.set_smart_enabled(False)
        self.async_write_ha_state()
        # Clear schedule and pause charger
        self._coordinator.schedule = []
        self._coordinator._update_schedule_sensors()
        if self._coordinator._transaction_active:
            await self._coordinator.charger.async_set_frc(1)

    async def _async_restore(self, value: bool) -> None:
        self._coordinator.set_smart_enabled(value)


class EvChargingChargeNowSwitch(_BaseSwitch):
    """Manual override: charge immediately regardless of price."""

    def __init__(self, coordinator: EvSmartChargingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_charge_now"
        self._attr_name = "EV Charging Charge Now"

    @property
    def is_on(self) -> bool:
        return self._coordinator.get_charge_now()

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last:
            await self._async_restore(last.state == "on")
        # charge_now resets to off on restart intentionally — don't trigger a rebuild

    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.set_charge_now(True)
        self.async_write_ha_state()
        await self._coordinator._async_apply_charger_command()

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.set_charge_now(False)
        self.async_write_ha_state()
        await self._coordinator._async_apply_charger_command()

    async def _async_restore(self, value: bool) -> None:
        self._coordinator.set_charge_now(value)


class EvChargingDaySwitch(_BaseSwitch):
    """Per-weekday enabled toggle."""

    def __init__(
        self,
        coordinator: EvSmartChargingCoordinator,
        entry: ConfigEntry,
        day: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._day = day
        self._attr_unique_id = f"{entry.entry_id}_{day}_enabled"
        self._attr_name = f"EV Charging {day.capitalize()} Enabled"

    @property
    def is_on(self) -> bool:
        return self._coordinator.get_day_enabled(self._day)

    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.set_day_enabled(self._day, True)
        self.async_write_ha_state()
        await self._coordinator._async_rebuild_schedule()

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.set_day_enabled(self._day, False)
        self.async_write_ha_state()
        await self._coordinator._async_rebuild_schedule()

    async def _async_restore(self, value: bool) -> None:
        self._coordinator.set_day_enabled(self._day, value)
