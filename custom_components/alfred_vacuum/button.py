"""Pulsanti."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlfredConfigEntry
from .const import A_EMPTY_BIN, A_RESET_FILTER, A_RESET_MAIN_BRUSH, A_RESET_SIDE_BRUSH
from .entity import AlfredEntity

ACTIONS = [
    ("empty_bin", A_EMPTY_BIN, "mdi:delete-empty", None),
    ("reset_main_brush", A_RESET_MAIN_BRUSH, "mdi:broom", EntityCategory.CONFIG),
    ("reset_side_brush", A_RESET_SIDE_BRUSH, "mdi:broom", EntityCategory.CONFIG),
    ("reset_filter", A_RESET_FILTER, "mdi:air-filter", EntityCategory.CONFIG),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: AlfredConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities: list[ButtonEntity] = [AlfredActionButton(coordinator, *spec) for spec in ACTIONS]
    entities += [AlfredCleanSelectedRoom(coordinator), AlfredRefreshMap(coordinator)]
    async_add_entities(entities)


class AlfredActionButton(AlfredEntity, ButtonEntity):
    def __init__(self, coordinator, key, action, icon, category) -> None:
        super().__init__(coordinator, key)
        self._action = action
        self._attr_icon = icon
        self._attr_entity_category = category

    async def async_press(self) -> None:
        await self.run(self.coordinator.robot.action(self._action))


class AlfredCleanSelectedRoom(AlfredEntity, ButtonEntity):
    """Pulisce la stanza scelta nel selettore 'Stanza da pulire'."""

    _attr_icon = "mdi:play-circle"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "clean_selected_room")

    async def async_press(self) -> None:
        state = self.coordinator.selected_room
        if not state or state == "Nessuna stanza":
            raise HomeAssistantError("Scegli prima una stanza nel selettore 'Stanza da pulire'")
        try:
            self.coordinator.resolve_room(state)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self.run(self.coordinator.async_clean_rooms([state], source="pulsante"))


class AlfredRefreshMap(AlfredEntity, ButtonEntity):
    """Rilegge la mappa e i nomi delle stanze."""

    _attr_icon = "mdi:map-search"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "refresh_map")

    async def async_press(self) -> None:
        await self.coordinator.async_refresh_map()
        if self.coordinator.map_error:
            raise HomeAssistantError(f"Mappa non letta: {self.coordinator.map_error}")
