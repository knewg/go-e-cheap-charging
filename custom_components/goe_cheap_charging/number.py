"""Number entities (target SoC per day) for GO-e Cheap Charging."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_CHARGE_NOW_SOC_LIMIT, DEFAULT_CHEAP_THRESHOLD, DEFAULT_MANUAL_KWH, DEFAULT_OPPORTUNISTIC_SOC_LIMIT, DEFAULT_PRICE_SPREAD_THRESHOLD, DEFAULT_TARGET_SOC, DOMAIN, WEEKDAYS
from .coordinator import ChargingCoordinator
from .entity import ev_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [TargetSoc(coordinator, entry, day) for day in WEEKDAYS]
        + [ManualKwh(coordinator, entry, day) for day in WEEKDAYS]
        + [CheapThreshold(coordinator, entry)]
        + [PriceSpreadThreshold(coordinator, entry)]
        + [OpportunisticSocLimit(coordinator, entry)]
        + [ChargeNowSocLimit(coordinator, entry)]
    )


class TargetSoc(RestoreEntity, NumberEntity):
    """Per-weekday target SoC slider."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: ChargingCoordinator,
        entry: ConfigEntry,
        day: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._day = day
        self._attr_unique_id = f"{entry.entry_id}_{day}_target_soc"
        self._attr_name = f"{day.capitalize()} Target SoC"
        self._attr_device_info = ev_device_info(entry)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable", ""):
            try:
                self._coordinator.set_day_target_soc(self._day, float(last.state))
            except ValueError:
                pass
        self._coordinator.schedule_pending_rebuild()

    @property
    def native_value(self) -> float:
        return float(self._coordinator.get_day_target_soc(self._day))

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.set_day_target_soc(self._day, value)
        self.async_write_ha_state()
        await self._coordinator._async_rebuild_schedule()


class CheapThreshold(RestoreEntity, NumberEntity):
    """Global cheap price threshold for opportunistic charging (SEK/kWh, 0 = disabled)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_min_value = 0.00
    _attr_native_max_value = 5.00
    _attr_native_step = 0.01
    _attr_native_unit_of_measurement = "SEK/kWh"
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: ChargingCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cheap_price_threshold"
        self._attr_name = "Cheap Price Threshold"
        self._attr_device_info = ev_device_info(entry)

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


class PriceSpreadThreshold(RestoreEntity, NumberEntity):
    """Global price spread threshold — if spread < this, charge the whole window."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_min_value = 0.00
    _attr_native_max_value = 2.00
    _attr_native_step = 0.01
    _attr_native_unit_of_measurement = "SEK/kWh"
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: ChargingCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_price_spread_threshold"
        self._attr_name = "Price Spread Threshold"
        self._attr_device_info = ev_device_info(entry)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable", ""):
            try:
                self._coordinator.set_price_spread_threshold(float(last.state))
            except ValueError:
                pass

    @property
    def native_value(self) -> float:
        return self._coordinator.get_price_spread_threshold()

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.set_price_spread_threshold(value)
        self.async_write_ha_state()
        await self._coordinator._async_rebuild_schedule()


class ManualKwh(RestoreEntity, NumberEntity):
    """Per-weekday manual kWh override (0 = use SoC-based calculation)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "kWh"
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: ChargingCoordinator,
        entry: ConfigEntry,
        day: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._day = day
        self._attr_unique_id = f"{entry.entry_id}_{day}_manual_kwh"
        self._attr_name = f"{day.capitalize()} Manual kWh"
        self._attr_device_info = ev_device_info(entry)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable", ""):
            try:
                self._coordinator.set_day_manual_kwh(self._day, float(last.state))
            except ValueError:
                pass
        self._coordinator.schedule_pending_rebuild()

    @property
    def native_value(self) -> float:
        return self._coordinator.get_day_manual_kwh(self._day)

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.set_day_manual_kwh(self._day, value)
        self.async_write_ha_state()
        await self._coordinator._async_rebuild_schedule()


class OpportunisticSocLimit(RestoreEntity, NumberEntity):
    """SoC cap for opportunistic cheap-price charging (default 80%)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: ChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_opportunistic_soc_limit"
        self._attr_name = "Opportunistic Charging SoC Limit"
        self._attr_device_info = ev_device_info(entry)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable", ""):
            try:
                self._coordinator.set_opportunistic_soc_limit(float(last.state))
            except ValueError:
                pass

    @property
    def native_value(self) -> float:
        return self._coordinator.get_opportunistic_soc_limit()

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.set_opportunistic_soc_limit(value)
        self.async_write_ha_state()
        await self._coordinator._async_apply_charger_command()


class ChargeNowSocLimit(RestoreEntity, NumberEntity):
    """SoC cap for manual charge_now override (default 80%)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: ChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_charge_now_soc_limit"
        self._attr_name = "Charge Now SoC Limit"
        self._attr_device_info = ev_device_info(entry)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable", ""):
            try:
                self._coordinator.set_charge_now_soc_limit(float(last.state))
            except ValueError:
                pass

    @property
    def native_value(self) -> float:
        return self._coordinator.get_charge_now_soc_limit()

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.set_charge_now_soc_limit(value)
        self.async_write_ha_state()
        await self._coordinator._async_apply_charger_command()
