"""Sensori."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlfredConfigEntry
from .const import (
    CHARGING_STATES,
    P_BATTERY,
    P_CHARGING,
    P_CLEANING_AREA,
    P_CLEANING_TIME,
    P_DUST_STATUS,
    P_FAULT,
    P_FAULTS,
    P_FILTER_LIFE,
    P_FILTER_TIME,
    P_MAIN_BRUSH_LIFE,
    P_MAIN_BRUSH_TIME,
    P_SIDE_BRUSH_LIFE,
    P_SIDE_BRUSH_TIME,
    P_STATUS,
    P_TASK_STATUS,
    P_TOTAL_AREA,
    P_TOTAL_COUNT,
    P_TOTAL_TIME,
    P_WORK_MODE,
    STATUS_NAMES,
)
from .entity import AlfredEntity


@dataclass(frozen=True, kw_only=True)
class AlfredSensorDescription(SensorEntityDescription):
    prop: tuple[int, int] | None = None
    convert: Callable[[Any], Any] | None = None


def _enum(mapping: dict[int, str]) -> Callable[[Any], Any]:
    return lambda v: mapping.get(v, str(v) if v is not None else None)


SENSORS: tuple[AlfredSensorDescription, ...] = (
    AlfredSensorDescription(
        key="battery", prop=P_BATTERY, device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
    ),
    AlfredSensorDescription(
        key="status", prop=P_STATUS, device_class=SensorDeviceClass.ENUM,
        options=sorted(set(STATUS_NAMES.values())), convert=_enum(STATUS_NAMES),
    ),
    AlfredSensorDescription(
        key="charging_state", prop=P_CHARGING, device_class=SensorDeviceClass.ENUM,
        options=sorted(set(CHARGING_STATES.values())), convert=_enum(CHARGING_STATES),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AlfredSensorDescription(
        key="cleaning_time", prop=P_CLEANING_TIME, device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    AlfredSensorDescription(
        key="cleaning_area", prop=P_CLEANING_AREA, device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
    ),
    AlfredSensorDescription(key="error", prop=P_FAULT, entity_category=EntityCategory.DIAGNOSTIC),
    AlfredSensorDescription(
        key="faults", prop=P_FAULTS, entity_category=EntityCategory.DIAGNOSTIC,
        convert=lambda v: v if v not in (None, "") else "0",
    ),
    AlfredSensorDescription(key="work_mode", prop=P_WORK_MODE, entity_category=EntityCategory.DIAGNOSTIC),
    AlfredSensorDescription(key="task_status", prop=P_TASK_STATUS, entity_category=EntityCategory.DIAGNOSTIC),
    AlfredSensorDescription(
        key="main_brush_left", prop=P_MAIN_BRUSH_TIME, device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AlfredSensorDescription(
        key="main_brush_life", prop=P_MAIN_BRUSH_LIFE, native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AlfredSensorDescription(
        key="side_brush_left", prop=P_SIDE_BRUSH_TIME, device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AlfredSensorDescription(
        key="side_brush_life", prop=P_SIDE_BRUSH_LIFE, native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AlfredSensorDescription(
        key="filter_life", prop=P_FILTER_LIFE, native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AlfredSensorDescription(
        key="filter_left", prop=P_FILTER_TIME, device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AlfredSensorDescription(
        key="total_time", prop=P_TOTAL_TIME, device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES, state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AlfredSensorDescription(
        key="total_count", prop=P_TOTAL_COUNT, state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AlfredSensorDescription(
        key="total_area", prop=P_TOTAL_AREA, device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS, state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AlfredSensorDescription(key="dust_status", prop=P_DUST_STATUS, entity_category=EntityCategory.DIAGNOSTIC),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: AlfredConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [AlfredSensor(coordinator, d) for d in SENSORS]
    entities += [AlfredRoomsSensor(coordinator), AlfredCurrentRoomSensor(coordinator)]
    async_add_entities(entities)


class AlfredSensor(AlfredEntity, SensorEntity):
    entity_description: AlfredSensorDescription

    def __init__(self, coordinator, description: AlfredSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        raw = self.prop_value(self.entity_description.prop)
        if self.entity_description.convert:
            return self.entity_description.convert(raw)
        return raw


class AlfredRoomsSensor(AlfredEntity, SensorEntity):
    """Numero di stanze note; nell'attributo 'stanze' l'elenco numero → nome."""

    _attr_icon = "mdi:floor-plan"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "rooms")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.rooms)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self.coordinator
        return {
            "stanze": {r.room_id: r.name for r in coord.rooms.values()},
            "tipi": {r.room_id: r.room_type for r in coord.rooms.values()},
            "mappa_aggiornata": coord.map_updated.isoformat() if coord.map_updated else None,
            "errore_mappa": coord.map_error,
        }


class AlfredCurrentRoomSensor(AlfredEntity, SensorEntity):
    """Stanza in cui si trova il robot (dall'ultima mappa letta)."""

    _attr_icon = "mdi:map-marker"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "current_room")

    @property
    def native_value(self) -> str | None:
        room = self.coordinator.current_room
        return room.name if room else None
