"""Number entities (target SoC per day) for EV Smart Charging."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_CHEAP_THRESHOLD, DEFAULT_TARGET_SOC, DOMAIN, WEEKDAYS
from .coordinator import EvSmartChargingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EvSmartChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [EvChargingTargetSoc(coordinator, entry, day) for day in WEEKDAYS]
        + [EvChargingCheapThreshold(coordinator, entry)]
    )


class EvChargingTargetSoc(RestoreEntity, NumberEntity):
    """Per-weekday target SoC slider."""

    _attr_should_poll = False
    _attr_native_min_value = 20
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: EvSmartChargingCoordinator,
        entry: ConfigEntry,
        day: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._day = day
        self._attr_unique_id = f"{entry.entry_id}_{day}_target_soc"
        self._attr_name = f"EV Charging {day.capitalize()} Target SoC"

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable", ""):
            try:
                self._coordinator.set_day_target_soc(self._day, float(last.state))
            except ValueError:
                pass

    @property
    def native_value(self) -> float:
        return float(self._coordinator.get_day_target_soc(self._day))

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.set_day_target_soc(self._day, value)
        self.async_write_ha_state()
        await self._coordinator._async_rebuild_schedule()


class EvChargingCheapThreshold(RestoreEntity, NumberEntity):
    """Global cheap price threshold for opportunistic charging (SEK/kWh, 0 = disabled)."""

    _attr_should_poll = False
    _attr_native_min_value = 0.00
    _attr_native_max_value = 5.00
    _attr_native_step = 0.01
    _attr_native_unit_of_measurement = "SEK/kWh"
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: EvSmartChargingCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cheap_price_threshold"
        self._attr_name = "EV Charging Cheap Price Threshold"

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable", ""):
            try:
                self._coordinator.set_cheap_threshold(float(last.state))
            except ValueError:
                pass

    @property
    def native_value(self) -> float:
        return self._coordinator.get_cheap_threshold()

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.set_cheap_threshold(value)
        self.async_write_ha_state()
        await self._coordinator._async_apply_charger_command()
