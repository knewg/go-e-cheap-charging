"""Shared device info helper for GO-e Cheap Charging entities."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


def ev_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="GO-e Cheap Charging",
        manufacturer="Custom",
        model="GO-e Cheap Charger",
    )
