"""Select entity for choosing active car (Kia cars or Guest)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import ACTIVE_CAR_GUEST, DOMAIN
from .coordinator import ChargingCoordinator
from .entity import ev_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ActiveCarSelect(coordinator, entry)])


class ActiveCarSelect(RestoreEntity, SelectEntity):
    """Dropdown to select the active car or Guest mode."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:car-electric"

    def __init__(self, coordinator: ChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_active_car"
        self._attr_name = "Active Car"
        self._attr_device_info = ev_device_info(entry)
        self._option_map: dict[str, tuple[str, str]] = {}  # label → (soc_entity_id, device_id)
        self._current_option: str = "Guest"
        self._attr_options: list[str] = ["Guest"]
        self._pending_restore: str | None = None

    def _build_option_map(self) -> dict[str, tuple[str, str]]:
        """Discover all Kia UVO cars and return label→(soc_entity_id, device_id) map."""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        # Collect one % sensor per device (first match wins).
        device_to_soc: dict[str, str] = {}
        for e in ent_reg.entities.values():
            if e.domain != "sensor" or not e.platform or "kia" not in e.platform.lower():
                continue
            state = self.hass.states.get(e.entity_id)
            if state and state.attributes.get("unit_of_measurement") == "%":
                if e.device_id and e.device_id not in device_to_soc:
                    device_to_soc[e.device_id] = e.entity_id

        options: dict[str, tuple[str, str]] = {}
        for device_id, soc_entity_id in device_to_soc.items():
            device = dev_reg.async_get(device_id)
            label = (device.name_by_user or device.name or soc_entity_id) if device else soc_entity_id
            options[label] = (soc_entity_id, device_id)
        options["Guest"] = (ACTIVE_CAR_GUEST, ACTIVE_CAR_GUEST)
        return options

    async def async_added_to_hass(self) -> None:
        self._option_map = self._build_option_map()
        self._attr_options = list(self._option_map.keys())

        last = await self.async_get_last_state()
        restored_label = last.state if last else None

        if restored_label and restored_label in self._option_map:
            # Happy path: label is known (including explicit "Guest").
            self._current_option = restored_label
        elif restored_label and restored_label != "Guest":
            # Car label exists in history but its entity isn't registered yet.
            # This is a startup race with kia_uvo: our integration loaded before
            # the car integration, so _build_option_map() found nothing.
            # Apply Guest temporarily and retry once HA finishes starting.
            self._current_option = "Guest"
            self._pending_restore = restored_label
            if self.hass.state != CoreState.running:
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._async_retry_restore
                )
            else:
                await self._async_retry_restore(None)
        else:
            # No history, or history was Guest: use first discovered car or Guest.
            non_guest = [label for label in self._option_map if label != "Guest"]
            self._current_option = non_guest[0] if non_guest else "Guest"

        soc_entity_id, device_id = self._option_map[self._current_option]
        self._coordinator.async_set_active_car(soc_entity_id, device_id)

    async def _async_retry_restore(self, _event: Event | None) -> None:
        """Retry car selection after HA fully starts (resolves kia_uvo startup race)."""
        self._option_map = self._build_option_map()
        self._attr_options = list(self._option_map.keys())

        if self._pending_restore and self._pending_restore in self._option_map:
            self._current_option = self._pending_restore
        else:
            non_guest = [label for label in self._option_map if label != "Guest"]
            self._current_option = non_guest[0] if non_guest else "Guest"

        self._pending_restore = None
        soc_entity_id, device_id = self._option_map[self._current_option]
        self._coordinator.async_set_active_car(soc_entity_id, device_id)
        self.async_write_ha_state()
        await self._coordinator._async_rebuild_schedule()

    @property
    def current_option(self) -> str:
        return self._current_option

    async def async_select_option(self, option: str) -> None:
        self._option_map = self._build_option_map()
        if option not in self._option_map:
            return
        self._current_option = option
        soc_entity_id, device_id = self._option_map[option]
        self._coordinator.async_set_active_car(soc_entity_id, device_id)
        self.async_write_ha_state()
        await self._coordinator._async_rebuild_schedule()
