"""Sensori binari."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlfredConfigEntry
from .const import P_WATERBOX
from .entity import AlfredEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: AlfredConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([AlfredWaterTank(entry.runtime_data.coordinator)])


class AlfredWaterTank(AlfredEntity, BinarySensorEntity):
    """Serbatoio dell'acqua montato."""

    _attr_icon = "mdi:water"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "water_tank")

    @property
    def is_on(self) -> bool | None:
        value = self.prop_value(P_WATERBOX)
        return None if value is None else bool(value)
