"""Kia UVO car driver — wraps kia_uvo HA integration service calls."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class KiaUvoDriver:
    """Reads SoC and sends commands via the kia_uvo HA integration."""

    def __init__(
        self, hass: HomeAssistant, soc_entity_id: str, device_id: str
    ) -> None:
        self.hass = hass
        self.soc_entity_id = soc_entity_id
        self.device_id = device_id

    def get_soc(self) -> float:
        """Return current battery SoC as a float percent, or 0.0 on failure."""
        state = self.hass.states.get(self.soc_entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            _LOGGER.warning("Car SoC entity %s unavailable", self.soc_entity_id)
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
