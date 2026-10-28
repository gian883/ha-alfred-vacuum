"""Selettori: potenza, acqua, stanza da pulire."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import AlfredConfigEntry
from .const import FAN_SPEEDS, MOP_LEVELS, P_FAN, P_MOP
from .entity import AlfredEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: AlfredConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            AlfredPropertySelect(coordinator, "fan_speed", P_FAN, FAN_SPEEDS, "mdi:fan"),
            AlfredPropertySelect(coordinator, "water_level", P_MOP, MOP_LEVELS, "mdi:water-percent"),
            AlfredRoomSelect(coordinator),
        ]
    )


class AlfredPropertySelect(AlfredEntity, SelectEntity):
    def __init__(self, coordinator, key, prop, mapping: dict[int, str], icon: str) -> None:
        super().__init__(coordinator, key)
        self._prop = prop
        self._mapping = mapping
        self._attr_options = list(mapping.values())
        self._attr_icon = icon

    @property
    def current_option(self) -> str | None:
        return self._mapping.get(self.prop_value(self._prop))

    async def async_select_option(self, option: str) -> None:
        ids = {v: k for k, v in self._mapping.items()}
        await self.run(self.coordinator.robot.set_property(self._prop, ids[option]))


class AlfredRoomSelect(AlfredEntity, SelectEntity, RestoreEntity):
    """Stanza scelta per il pulsante 'Pulisci stanza selezionata'."""

    _attr_icon = "mdi:door"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "selected_room")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable"):
            self.coordinator.selected_room = last.state

    @property
    def options(self) -> list[str]:
        names = [r.name for r in self.coordinator.rooms.values()]
        return names or ["Nessuna stanza"]

    @property
    def current_option(self) -> str | None:
        selected = self.coordinator.selected_room
        if selected not in self.options:
            selected = self.options[0]
        return selected

    async def async_select_option(self, option: str) -> None:
        self.coordinator.selected_room = option
        self.async_write_ha_state()
