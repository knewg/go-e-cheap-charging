"""Go-e charger MQTT driver — API v2 (individual topics per parameter)."""
from __future__ import annotations

import json
import logging

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant

from ..const import MQTT_STATUS_TOPIC

_LOGGER = logging.getLogger(__name__)


class GoeCharger:
    """Encapsulates all Go-e MQTT interaction (API v2)."""

    def __init__(self, hass: HomeAssistant, serial: str) -> None:
        self.hass = hass
        self.serial = serial
        self.status_topic = MQTT_STATUS_TOPIC.format(serial=serial)

    def extract_key(self, topic: str) -> str | None:
        """Extract parameter key from go-e API v2 topic.

        Topic format: go-eCharger/{serial}/{key}
        Ignores /set and /result subtopics (commands/responses).
        """
        prefix = f"go-eCharger/{self.serial}/"
        if not topic.startswith(prefix):
            return None
        remainder = topic[len(prefix):]
        # Only handle top-level keys, not /set or /result
        if "/" in remainder:
            return None
        return remainder

    async def async_set_frc(self, value: int) -> None:
        """Set force-charge mode. 1=force stop, 2=force charge."""
        await self._async_publish("frc", value)

    async def async_set_amp(self, amps: int) -> None:
        """Set charging current in amps."""
        await self._async_publish("amp", amps)

    async def async_start_transaction(self, force_charge: bool = True) -> None:
        """Start a new charging transaction then set frc."""
        await self._async_publish("trx", 1)
        await self._async_publish("frc", 2 if force_charge else 1)

    async def _async_publish(self, key: str, value: int) -> None:
        topic = f"go-eCharger/{self.serial}/{key}/set"
        payload = json.dumps(value)
        _LOGGER.debug("go-e MQTT → %s : %s", topic, payload)
        await mqtt.async_publish(self.hass, topic, payload)
