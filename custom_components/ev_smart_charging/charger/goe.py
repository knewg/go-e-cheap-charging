"""Go-e charger MQTT driver."""
from __future__ import annotations

import json
import logging

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant

from ..const import MQTT_COMMAND_TOPIC, MQTT_STATUS_TOPIC

_LOGGER = logging.getLogger(__name__)


class GoeCharger:
    """Encapsulates all Go-e MQTT interaction."""

    def __init__(self, hass: HomeAssistant, serial: str) -> None:
        self.hass = hass
        self.serial = serial
        self.status_topic = MQTT_STATUS_TOPIC.format(serial=serial)
        self.command_topic = MQTT_COMMAND_TOPIC.format(serial=serial)

    async def async_set_frc(self, value: int) -> None:
        """Set force-charge mode. 1=force stop, 2=force charge."""
        await self._async_publish({"frc": value})

    async def async_set_amp(self, amps: int) -> None:
        """Set charging current in amps."""
        await self._async_publish({"amp": amps})

    async def async_start_transaction(self, force_charge: bool = True) -> None:
        """Start a new charging transaction.

        Combines trx=1 with the desired frc value in one publish so the charger
        starts (or stays paused) atomically.
        """
        frc = 2 if force_charge else 1
        await self._async_publish({"trx": 1, "frc": frc})

    async def _async_publish(self, payload: dict) -> None:
        raw = json.dumps(payload)
        _LOGGER.debug("go-e MQTT → %s : %s", self.command_topic, raw)
        await mqtt.async_publish(self.hass, self.command_topic, raw)

    @staticmethod
    def parse_status(raw: str) -> dict:
        """Parse a raw MQTT status JSON string. Returns {} on failure."""
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.warning("Failed to parse go-e status: %r", raw)
            return {}
