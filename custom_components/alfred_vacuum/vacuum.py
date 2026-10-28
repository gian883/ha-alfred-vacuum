"""Entità aspirapolvere."""

from __future__ import annotations

from typing import Any

from homeassistant.components.vacuum import StateVacuumEntity, VacuumActivity, VacuumEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlfredConfigEntry
from .const import (
    A_DOCK,
    A_LOCATE,
    A_PAUSE,
    A_START,
    A_STOP,
    FAN_SPEEDS,
    P_BATTERY,
    P_FAN,
    P_FAULT,
    P_STATUS,
    STATUS_NAMES,
)
from .entity import AlfredEntity

CLEANING = {1, 7, 11, 12}
DOCKED = {6, 13, 14}
PAUSED = {3, 4}
RETURNING = {5}


async def async_setup_entry(
    hass: HomeAssistant, entry: AlfredConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([AlfredVacuum(entry.runtime_data.coordinator)])


class AlfredVacuum(AlfredEntity, StateVacuumEntity):
    """Il robot."""

    _attr_name = None
    _attr_fan_speed_list = list(FAN_SPEEDS.values())
    _attr_supported_features = (
        VacuumEntityFeature.START
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.STATE
        | VacuumEntityFeature.SEND_COMMAND
    )

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "vacuum")

    @property
    def activity(self) -> VacuumActivity | None:
        status = self.prop_value(P_STATUS)
        if status is None:
            return None
        fault = self.prop_value(P_FAULT)
        if fault not in (None, 0) and status not in DOCKED:
            return VacuumActivity.ERROR
        if status in CLEANING:
            return VacuumActivity.CLEANING
        if status in DOCKED:
            return VacuumActivity.DOCKED
        if status in PAUSED:
            return VacuumActivity.PAUSED
        if status in RETURNING:
            return VacuumActivity.RETURNING
        return VacuumActivity.IDLE

    @property
    def fan_speed(self) -> str | None:
        return FAN_SPEEDS.get(self.prop_value(P_FAN))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self.coordinator
        current = coord.current_room
        return {
            "stato_robot": STATUS_NAMES.get(self.prop_value(P_STATUS), self.prop_value(P_STATUS)),
            "batteria": self.prop_value(P_BATTERY),
            "stanze": {r.room_id: r.name for r in coord.rooms.values()},
            "stanza_attuale": current.name if current else None,
            "mappa_aggiornata": coord.map_updated.isoformat() if coord.map_updated else None,
            "errore_mappa": coord.map_error,
            "collegamento": coord.robot.last_route,
        }

    async def async_start(self) -> None:
        await self.run(self.coordinator.robot.action(A_START))

    async def async_pause(self) -> None:
        await self.run(self.coordinator.robot.action(A_PAUSE))

    async def async_stop(self, **kwargs: Any) -> None:
        await self.run(self.coordinator.robot.action(A_STOP))

    async def async_return_to_base(self, **kwargs: Any) -> None:
        await self.run(self.coordinator.robot.action(A_DOCK))

    async def async_locate(self, **kwargs: Any) -> None:
        await self.run(self.coordinator.robot.action(A_LOCATE))

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        ids = {v: k for k, v in FAN_SPEEDS.items()}
        if fan_speed not in ids:
            raise ServiceValidationError(f"Potenza non valida: {fan_speed}")
        await self.run(self.coordinator.robot.set_property(P_FAN, ids[fan_speed]))

    async def async_send_command(self, command: str, params: dict | list | None = None, **kwargs: Any) -> None:
        """'clean_rooms' con params {'rooms': [...]} oppure una lista di stanze."""
        if command not in ("clean_rooms", "app_segment_clean"):
            raise ServiceValidationError("Comandi supportati: clean_rooms")
        rooms = params.get("rooms") if isinstance(params, dict) else params
        try:
            ids = [self.coordinator.resolve_room(r) for r in (rooms or [])]
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        await self.run(self.coordinator.robot.clean_rooms(ids, self.coordinator.data or {}))
