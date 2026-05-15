"""Binary sensor entities for GO-e Cheap Charging."""
from __future__ import annotations

import math
from datetime import datetime

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CAR_IDLE, DOMAIN
from .coordinator import ChargingCoordinator
from .entity import ev_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ChargerConnectionNeededSensor(coordinator, entry)])


class ChargerConnectionNeededSensor(BinarySensorEntity):
    """On when charging is scheduled but the cable is not connected."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PLUG
    _attr_icon = "mdi:ev-plug-type2"

    def __init__(self, coordinator: ChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_cable_needed"
        self._attr_name = "Charger Connection Needed"
        self._attr_device_info = ev_device_info(entry)

    @property
    def is_on(self) -> bool:
        if self._coordinator.car_state != CAR_IDLE:
            return False
        if not self._coordinator._smart_enabled:
            return False
        now = dt_util.utcnow()
        return any(
            slot["selected"] and slot["end"] > now
            for slot in self._coordinator.schedule
        )

    @property
    def extra_state_attributes(self) -> dict:
        now = dt_util.utcnow()
        next_start: datetime | None = None
        for slot in self._coordinator.schedule:
            if slot["selected"] and slot["end"] > now:
                next_start = slot["start"]
                break

        if next_start is None:
            return {"next_slot_start": None, "minutes_until_next_slot": None}

        minutes = math.ceil((next_start - now).total_seconds() / 60)
        return {
            "next_slot_start": next_start.isoformat(),
            "minutes_until_next_slot": max(0, minutes),
        }
