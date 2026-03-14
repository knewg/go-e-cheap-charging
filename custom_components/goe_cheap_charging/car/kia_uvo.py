"""Kia UVO car driver — wraps kia_uvo HA integration service calls."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


class KiaUvoDriver:
    """Reads SoC and sends commands via the kia_uvo HA integration."""

    def __init__(self, hass: HomeAssistant, device_id: str) -> None:
        self.hass = hass
        self.device_id = device_id

    @property
    def soc_entity_id(self) -> str | None:
        """Return the SoC sensor entity ID, discovered from the device registry."""
        registry = er.async_get(self.hass)
        entity = next(
            (
                e
                for e in er.async_entries_for_device(registry, self.device_id)
                if e.domain == "sensor" and e.entity_id.endswith("_ev_battery_level")
            ),
            None,
        )
        return entity.entity_id if entity is not None else None

    def get_soc(self) -> float:
        """Return current battery SoC as a float percent, or 0.0 on failure."""
        entity_id = self.soc_entity_id
        if entity_id is None:
            _LOGGER.warning("Car SoC entity not found in device registry for device %s", self.device_id)
            return 0.0
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            _LOGGER.warning("Car SoC entity %s unavailable", entity_id)
            return 0.0
        try:
            return float(state.state)
        except ValueError:
            _LOGGER.warning("Cannot parse car SoC state: %r", state.state)
            return 0.0

    async def async_force_update(self) -> None:
        """Request a fresh data pull from the Kia cloud."""
        await self.hass.services.async_call(
            "kia_uvo",
            "force_update",
            {"device_id": self.device_id},
            blocking=False,
        )

    def get_charge_limit(self) -> int | None:
        """Return the car's current AC charge limit in percent, or None if unavailable."""
        registry = er.async_get(self.hass)
        entity = next(
            (
                e
                for e in er.async_entries_for_device(registry, self.device_id)
                if e.domain == "number" and e.entity_id.endswith("ac_charging_limit")
            ),
            None,
        )
        if entity is None:
            return None
        state = self.hass.states.get(entity.entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return int(float(state.state))
        except ValueError:
            return None

    async def async_set_charge_limit(self, limit_pct: int) -> None:
        """Set the car's AC charge limit via kia_uvo service (safety net).

        This is best-effort — if the service is unavailable or the car doesn't
        support it the schedule still controls charging via the charger.
        """
        try:
            await self.hass.services.async_call(
                "kia_uvo",
                "set_charge_limits",
                {"device_id": self.device_id, "ac_limit": limit_pct, "dc_limit": limit_pct},
                blocking=False,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "kia_uvo.set_charge_limits unavailable — car charge limit not updated"
            )
