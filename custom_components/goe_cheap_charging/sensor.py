"""Sensor entities (status display) for GO-e Cheap Charging."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ChargingCoordinator
from .entity import ev_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ChargingCoordinator = hass.data[DOMAIN][entry.entry_id]

    schedule_sensor = ScheduleSensor(coordinator, entry)
    next_slot_sensor = NextSlotSensor(coordinator, entry)

    # Give coordinator references so it can push state updates
    coordinator._schedule_sensor = schedule_sensor
    coordinator._next_slot_sensor = next_slot_sensor

    async_add_entities([schedule_sensor, next_slot_sensor])





class ScheduleSensor(SensorEntity):
    """Human-readable charging schedule summary."""

    _attr_should_poll = False
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: ChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_schedule"
        self._attr_name = "Cheap Charging Schedule"
        self._attr_device_info = ev_device_info(entry)

    @property
    def native_value(self) -> str:
        return self._coordinator.get_schedule_summary()

    @property
    def extra_state_attributes(self) -> dict:
        selected = [s for s in self._coordinator.schedule if s["selected"]]
        return {
            "slots": [
                {
                    "start": s["start"].isoformat(),
                    "end": s["end"].isoformat(),
                    "price": round(s["price"], 4),
                }
                for s in sorted(selected, key=lambda s: s["start"])
            ]
        }


class NextSlotSensor(SensorEntity):
    """Timestamp of the next selected charging slot."""

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator: ChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_next_slot"
        self._attr_name = "Cheap Charging Next Slot"
        self._attr_device_info = ev_device_info(entry)

    @property
    def native_value(self) -> datetime | None:
        return self._coordinator.get_next_slot_time()
