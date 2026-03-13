"""Time entities (departure pickers) for EV Smart Charging."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, WEEKDAYS
from .coordinator import EvSmartChargingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EvSmartChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [EvChargingDepartureTime(coordinator, entry, day) for day in WEEKDAYS]
    )


class EvChargingDepartureTime(RestoreEntity, TimeEntity):
    """Departure time picker for one weekday."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: EvSmartChargingCoordinator,
        entry: ConfigEntry,
        day: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._day = day
        self._attr_unique_id = f"{entry.entry_id}_{day}_departure"
        self._attr_name = f"EV Charging {day.capitalize()} Departure"

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable", ""):
            try:
                parts = [int(x) for x in last.state.split(":")]
                t = time(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)
                self._coordinator.set_day_departure(self._day, t)
            except (ValueError, IndexError):
                pass
        self._coordinator.schedule_pending_rebuild()

    @property
    def native_value(self) -> time | None:
        return self._coordinator.get_day_departure(self._day)

    async def async_set_value(self, value: time) -> None:
        self._coordinator.set_day_departure(self._day, value)
        self.async_write_ha_state()
        await self._coordinator._async_rebuild_schedule()
