"""Dati di diagnostica (senza segreti)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant

from . import AlfredConfigEntry
from .const import CONF_DID, CONF_SERVICE_TOKEN, CONF_SSECURITY, CONF_USER_ID

TO_REDACT = {CONF_TOKEN, CONF_HOST, CONF_DID, CONF_SERVICE_TOKEN, CONF_SSECURITY, CONF_USER_ID, "object_name"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: AlfredConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "properties": {f"{k[0]}-{k[1]}": v for k, v in (coordinator.data or {}).items()},
        "route": coordinator.robot.last_route,
        "rooms": {r.room_id: {"name": r.name, "type": r.room_type} for r in coordinator.rooms.values()},
        "map": async_redact_data(coordinator.map_details, TO_REDACT),
    }
