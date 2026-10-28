"""Interruttori di configurazione."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlfredConfigEntry
from .const import P_AUTO_EMPTY, P_CARPET, P_DND, P_RESUME
from .entity import AlfredEntity

# (chiave, proprietà, icona, il valore è booleano?)
SWITCHES = [
    ("auto_empty", P_AUTO_EMPTY, "mdi:delete-restore", False),
    ("carpet_boost", P_CARPET, "mdi:rug", False),
    ("resume_after_charge", P_RESUME, "mdi:battery-sync", False),
    ("do_not_disturb", P_DND, "mdi:minus-circle-off", True),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: AlfredConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(AlfredSwitch(coordinator, *spec) for spec in SWITCHES)


class AlfredSwitch(AlfredEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key, prop, icon, boolean) -> None:
        super().__init__(coordinator, key)
        self._prop = prop
        self._attr_icon = icon
        self._boolean = boolean

    @property
    def is_on(self) -> bool | None:
        value = self.prop_value(self._prop)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.run(self.coordinator.robot.set_property(self._prop, True if self._boolean else 1))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.run(self.coordinator.robot.set_property(self._prop, False if self._boolean else 0))
