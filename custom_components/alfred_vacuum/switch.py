"""Interruttori di configurazione."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import AlfredConfigEntry
from .automations import FLAG_AWAY, FLAG_SMART
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
    entities: list[SwitchEntity] = [AlfredSwitch(coordinator, *spec) for spec in SWITCHES]
    entities += [
        AlfredAutomationSwitch(coordinator, "away_cleaning", FLAG_AWAY, "mdi:home-export-outline"),
        AlfredAutomationSwitch(coordinator, "smart_cleaning", FLAG_SMART, "mdi:calendar-refresh"),
    ]
    async_add_entities(entities)


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


class AlfredAutomationSwitch(AlfredEntity, SwitchEntity, RestoreEntity):
    """Accende/spegne un'automazione integrata (stato ricordato ai riavvii, spenta di default)."""

    def __init__(self, coordinator, key: str, flag: str, icon: str) -> None:
        super().__init__(coordinator, key)
        self._flag = flag
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self.coordinator.flags[self._flag] = bool(last and last.state == "on")

    @property
    def is_on(self) -> bool:
        return self.coordinator.flags.get(self._flag, False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = self._manager()
        return {"ultimo_esito": manager.last_result if manager else None}

    def _manager(self):
        data = self.coordinator.entry.runtime_data
        return data.away if self._flag == FLAG_AWAY else data.smart

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.flags[self._flag] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.flags[self._flag] = False
        self.async_write_ha_state()
