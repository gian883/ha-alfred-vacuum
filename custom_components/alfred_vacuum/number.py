"""Valori numerici: volume e svuotamento automatico."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlfredConfigEntry
from .const import P_AUTO_EMPTY_EVERY, P_VOLUME
from .entity import AlfredEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: AlfredConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            AlfredNumber(coordinator, "volume", P_VOLUME, 0, 100, 10, PERCENTAGE, "mdi:volume-high"),
            AlfredNumber(coordinator, "auto_empty_every", P_AUTO_EMPTY_EVERY, 1, 10, 1, None, "mdi:delete-restore"),
        ]
    )


class AlfredNumber(AlfredEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, key, prop, low, high, step, unit, icon) -> None:
        super().__init__(coordinator, key)
        self._prop = prop
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    @property
    def native_value(self) -> float | None:
        value = self.prop_value(self._prop)
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        await self.run(self.coordinator.robot.set_property(self._prop, int(value)))
