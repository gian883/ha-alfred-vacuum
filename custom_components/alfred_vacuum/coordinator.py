"""Aggiornamento periodico dello stato e delle stanze."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .cloud import CloudAuthError, CloudError
from .const import (
    CONF_MAP_INTERVAL,
    CONF_ROOM_NAMES,
    CONF_SCAN_INTERVAL,
    DEFAULT_MAP_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    P_STATUS,
    STATUS_CHARGED,
    STATUS_CHARGING,
)
from .device import AlfredRobot, RobotError
from .map_decoder import MapData, Room, describe

_LOGGER = logging.getLogger(__name__)


def parse_room_overrides(text: str | None) -> dict[int, str]:
    """'1=Soggiorno, 4=Cucina' -> {1: 'Soggiorno', 4: 'Cucina'}."""
    result: dict[int, str] = {}
    for part in (text or "").replace("\n", ",").split(","):
        if "=" not in part:
            continue
        key, _, name = part.partition("=")
        try:
            result[int(key.strip())] = name.strip()
        except ValueError:
            continue
    return {k: v for k, v in result.items() if v}


class AlfredCoordinator(DataUpdateCoordinator[dict[tuple[int, int], Any]]):
    """Legge lo stato del robot e, ogni tanto, la mappa."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, robot: AlfredRobot) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=interval),
        )
        self.robot = robot
        self.entry = entry
        self.map: MapData | None = None
        self.map_details: dict[str, Any] = {}
        self.map_updated: datetime | None = None
        self.map_error: str | None = None
        self._last_status: Any = None
        self.selected_room: str | None = None

    # --- stanze ----------------------------------------------------------
    @property
    def overrides(self) -> dict[int, str]:
        return parse_room_overrides(self.entry.options.get(CONF_ROOM_NAMES))

    @property
    def rooms(self) -> dict[int, Room]:
        """Stanze lette dalla mappa, con eventuali nomi scelti nelle opzioni."""
        rooms: dict[int, Room] = {}
        if self.map:
            rooms = {k: Room(v.room_id, v.name, v.room_type, v.custom_name) for k, v in self.map.rooms.items()}
        for room_id, name in self.overrides.items():
            if room_id in rooms:
                rooms[room_id].name = name
            else:
                rooms[room_id] = Room(room_id, name)
        return dict(sorted(rooms.items()))

    def resolve_room(self, value: str | int) -> int:
        """Accetta un numero o un nome ('cucina', 'Bagno') e restituisce il numero."""
        text = str(value).strip()
        if text.isdigit():
            return int(text)
        wanted = text.casefold()
        for room in self.rooms.values():
            if room.name.casefold() == wanted:
                return room.room_id
        for room in self.rooms.values():
            if wanted in room.name.casefold():
                return room.room_id
        raise ValueError(f"Stanza '{value}' non trovata. Stanze note: {', '.join(r.name for r in self.rooms.values()) or 'nessuna'}")

    @property
    def current_room(self) -> Room | None:
        if not self.map:
            return None
        room_id = self.map.robot_room
        return self.rooms.get(room_id) if room_id is not None else None

    async def async_refresh_map(self) -> dict[str, Any]:
        """Rilegge la mappa dal robot. Restituisce un riassunto."""
        try:
            data, details = await self.robot.request_map()
        except (RobotError, CloudError) as err:
            self.map_error = str(err)
            self.map_details = {"error": str(err)}
            return self.map_details
        self.map_details = details
        if data is not None:
            self.map = data
            self.map_updated = dt_util.utcnow()
            self.map_error = None
            details = {**details, **describe(data)}
            self.map_details = details
        else:
            self.map_error = details.get("error")
        self.async_update_listeners()
        return details

    # --- stato -----------------------------------------------------------
    async def _async_update_data(self) -> dict[tuple[int, int], Any]:
        try:
            data = await self.robot.get_properties()
        except CloudAuthError as err:
            raise ConfigEntryAuthFailed("Sessione Xiaomi scaduta, rifai il login") from err
        except (RobotError, CloudError) as err:
            raise UpdateFailed(str(err)) from err

        status = data.get(P_STATUS)
        returned_home = (
            self._last_status not in (None, STATUS_CHARGING, STATUS_CHARGED)
            and status in (STATUS_CHARGING, STATUS_CHARGED)
        )
        self._last_status = status
        map_interval = timedelta(minutes=self.entry.options.get(CONF_MAP_INTERVAL, DEFAULT_MAP_INTERVAL))
        stale = self.map_updated is None or dt_util.utcnow() - self.map_updated > map_interval
        if not self._map_busy and (returned_home or (stale and self._map_attempt_due())):
            self.hass.async_create_task(self._background_map())
        return data

    _map_attempts: int = 0
    _map_busy: bool = False

    def _map_attempt_due(self) -> bool:
        """Senza errori legge subito; dopo un errore ritenta ogni 40 aggiornamenti."""
        if not self.map_error:
            return True
        self._map_attempts += 1
        return self._map_attempts % 40 == 0

    async def _background_map(self) -> None:
        self._map_busy = True
        try:
            await self.async_refresh_map()
        except Exception:  # noqa: BLE001 - la mappa non deve mai bloccare lo stato
            _LOGGER.exception("Lettura della mappa non riuscita")
        finally:
            self._map_busy = False
