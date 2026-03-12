"""Sensor entities (status display) for EV Smart Charging."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EvSmartChargingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EvSmartChargingCoordinator = hass.data[DOMAIN][entry.entry_id]

    schedule_sensor = EvChargingScheduleSensor(coordinator, entry)
    next_slot_sensor = EvChargingNextSlotSensor(coordinator, entry)

    # Give coordinator references so it can push state updates
    coordinator._schedule_sensor = schedule_sensor
    coordinator._next_slot_sensor = next_slot_sensor

    async_add_entities([schedule_sensor, next_slot_sensor])


class EvChargingScheduleSensor(SensorEntity):
    """Human-readable charging schedule summary."""

    _attr_should_poll = False
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: EvSmartChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_schedule"
        self._attr_name = "EV Charging Schedule"

    @property
    def native_value(self) -> str:
        return self._coordinator.get_schedule_summary()


class EvChargingNextSlotSensor(SensorEntity):
    """Timestamp of the next selected charging slot."""

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator: EvSmartChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_next_slot"
        self._attr_name = "EV Charging Next Slot"

    @property
    def native_value(self) -> datetime | None:
        return self._coordinator.get_next_slot_time()
