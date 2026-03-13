"""Select entity for choosing active car (Kia cars or Guest)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import ACTIVE_CAR_GUEST, CONF_CAR_SOC_ENTITY, DOMAIN
from .coordinator import EvSmartChargingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EvSmartChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EvCarSelectEntity(coordinator, entry)])


class EvCarSelectEntity(RestoreEntity, SelectEntity):
    """Dropdown to select the active car or Guest mode."""

    _attr_should_poll = False
    _attr_icon = "mdi:car-electric"

    def __init__(self, coordinator: EvSmartChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_active_car"
        self._attr_name = "EV Charging Active Car"
        self._option_map: dict[str, tuple[str, str]] = {}  # label → (soc_entity_id, device_id)
        self._current_option: str = "Guest"
        self._attr_options: list[str] = ["Guest"]

    def _build_option_map(self) -> dict[str, tuple[str, str]]:
        """Discover all Kia UVO cars and return label→(soc_entity_id, device_id) map."""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        configured_soc = self._entry.data[CONF_CAR_SOC_ENTITY]
        # Collect all candidate % sensors per device; prefer the configured entity.
        device_to_soc: dict[str, str] = {}
        for e in ent_reg.entities.values():
            if e.domain != "sensor" or not e.platform or "kia" not in e.platform.lower():
                continue
            state = self.hass.states.get(e.entity_id)
            if state and state.attributes.get("unit_of_measurement") == "%":
                if e.device_id:
                    # Always prefer the explicitly configured entity over any other
                    if e.device_id not in device_to_soc or e.entity_id == configured_soc:
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
        if last and last.state in self._option_map:
            self._current_option = last.state
        else:
            primary_soc = self._entry.data[CONF_CAR_SOC_ENTITY]
            for label, (eid, _did) in self._option_map.items():
                if eid == primary_soc:
                    self._current_option = label
                    break
            else:
                self._current_option = "Guest"

        soc_entity_id, device_id = self._option_map[self._current_option]
        self._coordinator.async_set_active_car(soc_entity_id, device_id)

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
