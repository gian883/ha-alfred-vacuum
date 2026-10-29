"""Alfred Vacuum: integrazione Home Assistant per il Dreame D10 Plus."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .cloud import CloudSession, XiaomiCloud
from .const import (
    CONF_CLOUD,
    CONF_COUNTRY,
    CONF_DID,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    DEFAULT_COUNTRY,
    DOMAIN,
    FAN_SPEEDS,
    MOP_LEVELS,
    SERVICE_CLEAN_OVERDUE,
    SERVICE_CLEAN_ROOMS,
    SERVICE_CLEAN_ZONE,
    SERVICE_REFRESH_MAP,
    SERVICE_SEND_COMMAND,
)
from .automations import AwayCleaning, SmartCleaning
from .coordinator import AlfredCoordinator
from .device import AlfredRobot, RobotError
from .miio import MiioClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.CAMERA,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class AlfredData:
    coordinator: AlfredCoordinator
    cloud: XiaomiCloud | None
    away: AwayCleaning
    smart: SmartCleaning


type AlfredConfigEntry = ConfigEntry[AlfredData]


def build_cloud(entry: ConfigEntry) -> XiaomiCloud | None:
    data = entry.data
    if not data.get(CONF_CLOUD):
        return None
    session = CloudSession(
        user_id=data[CONF_USER_ID],
        service_token=data[CONF_SERVICE_TOKEN],
        ssecurity=data[CONF_SSECURITY],
        country=data.get(CONF_COUNTRY, DEFAULT_COUNTRY),
    )
    return XiaomiCloud(session.country, session)


async def async_setup_entry(hass: HomeAssistant, entry: AlfredConfigEntry) -> bool:
    cloud = build_cloud(entry)
    local = None
    if entry.data.get(CONF_HOST) and entry.data.get(CONF_TOKEN):
        local = MiioClient(entry.data[CONF_HOST], entry.data[CONF_TOKEN])
    robot = AlfredRobot(str(entry.data[CONF_DID]), local, cloud)
    coordinator = AlfredCoordinator(hass, entry, robot)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    away = AwayCleaning(hass, coordinator)
    smart = SmartCleaning(hass, coordinator)
    entry.runtime_data = AlfredData(coordinator, cloud, away, smart)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(away.async_start())
    entry.async_on_unload(smart.async_start())
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: AlfredConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AlfredConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and entry.runtime_data.cloud:
        await entry.runtime_data.cloud.close()
    return unloaded


# --- servizi ---------------------------------------------------------------

def _coordinator_for(hass: HomeAssistant, call: ServiceCall) -> AlfredCoordinator:
    return _data_for(hass, call).coordinator


def _data_for(hass: HomeAssistant, call: ServiceCall) -> AlfredData:
    """Trova il robot indicato nella chiamata (o l'unico configurato)."""
    entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    if not entries:
        raise ServiceValidationError("Nessun robot Alfred configurato")
    entity_ids = call.data.get("entity_id") or []
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    if entity_ids:
        registry = er.async_get(hass)
        for entity_id in entity_ids:
            ent = registry.async_get(entity_id)
            if ent and ent.config_entry_id:
                for entry in entries:
                    if entry.entry_id == ent.config_entry_id:
                        return entry.runtime_data
        raise ServiceValidationError(f"Entità {entity_ids} non appartiene ad Alfred Vacuum")
    if len(entries) > 1:
        raise ServiceValidationError("Più robot configurati: indica entity_id")
    return entries[0].runtime_data


CLEAN_ROOMS_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): cv.entity_ids,
        vol.Required("rooms"): vol.All(cv.ensure_list, [vol.Any(int, cv.string)]),
        vol.Optional("repeats", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
        vol.Optional("fan_speed"): vol.In(["Silenziosa", "Standard", "Forte", "Turbo"]),
        vol.Optional("water"): vol.In(["Bassa", "Media", "Alta"]),
        vol.Optional("source"): cv.string,
    }
)

def _zones(value: Any) -> list[list[float]]:
    """Accetta [[x1,y1,x2,y2], ...] oppure una sola zona [x1,y1,x2,y2] (anche come testo JSON)."""
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except ValueError as err:
            raise vol.Invalid("zone non leggibili") from err
    value = cv.ensure_list(value)
    if value and not isinstance(value[0], (list, tuple)):
        value = [value]
    zones = []
    for zone in value:
        try:
            numbers = [float(v) for v in zone]
        except (TypeError, ValueError) as err:
            raise vol.Invalid(f"zona non valida: {zone}") from err
        if len(numbers) < 4:
            raise vol.Invalid("una zona è [x1, y1, x2, y2]")
        zones.append(numbers[:4])
    return zones


CLEAN_ZONE_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): cv.entity_ids,
        vol.Required("zones"): _zones,
        vol.Optional("repeats", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
        vol.Optional("fan_speed"): vol.In(["Silenziosa", "Standard", "Forte", "Turbo"]),
        vol.Optional("water"): vol.In(["Bassa", "Media", "Alta"]),
        vol.Optional("source"): cv.string,
    }
)

CLEAN_OVERDUE_SCHEMA = vol.Schema(
    {vol.Optional("entity_id"): cv.entity_ids, vol.Optional("dry_run", default=False): cv.boolean}
)

SEND_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): cv.entity_ids,
        vol.Required("command"): vol.In(["get_properties", "set_property", "action"]),
        vol.Required("siid"): vol.Coerce(int),
        vol.Required("id"): vol.Coerce(int),
        vol.Optional("value"): object,
        vol.Optional("params", default=[]): list,
    }
)

REFRESH_SCHEMA = vol.Schema({vol.Optional("entity_id"): cv.entity_ids})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Registra i servizi una volta sola."""
    fan_ids = {v: k for k, v in FAN_SPEEDS.items()}
    mop_ids = {v: k for k, v in MOP_LEVELS.items()}

    async def clean_rooms(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator_for(hass, call)
        try:
            names = await coordinator.async_clean_rooms(
                call.data["rooms"],
                source=call.data.get("source", "servizio"),
                repeats=call.data["repeats"],
                fan=fan_ids.get(call.data.get("fan_speed")),
                water=mop_ids.get(call.data.get("water")),
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except RobotError as err:
            raise HomeAssistantError(str(err)) from err
        return {"stanze": names}

    async def clean_zone(call: ServiceCall) -> None:
        coordinator = _coordinator_for(hass, call)
        zones = call.data["zones"]
        try:
            await coordinator.async_clean_zones(
                zones,
                source=call.data.get("source", "zona sulla mappa"),
                repeats=call.data["repeats"],
                fan=fan_ids.get(call.data.get("fan_speed")),
                water=mop_ids.get(call.data.get("water")),
            )
        except RobotError as err:
            raise HomeAssistantError(str(err)) from err

    async def clean_overdue(call: ServiceCall) -> ServiceResponse:
        data = _data_for(hass, call)
        if call.data.get("dry_run"):
            return {
                "stanze_trascurate": [
                    {"stanza": n, "ultima_pulizia": d.isoformat() if d else None} for n, d in data.smart.candidates()
                ]
            }
        return {"esito": await data.smart.async_run(force=True)}

    async def refresh_map(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator_for(hass, call)
        details = await coordinator.async_refresh_map()
        return {"result": _jsonable(details)}

    async def send_command(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator_for(hass, call)
        siid, item = call.data["siid"], call.data["id"]
        try:
            if call.data["command"] == "get_properties":
                values = await coordinator.robot.get_properties([(siid, item)])
                result: Any = {f"{k[0]}-{k[1]}": v for k, v in values.items()}
            elif call.data["command"] == "set_property":
                await coordinator.robot.set_property((siid, item), call.data.get("value"))
                result = "ok"
            else:
                result = await coordinator.robot.action((siid, item), call.data.get("params") or [])
        except RobotError as err:
            raise HomeAssistantError(str(err)) from err
        return {"route": coordinator.robot.last_route, "result": _jsonable(result)}

    hass.services.async_register(
        DOMAIN, SERVICE_CLEAN_ROOMS, clean_rooms, schema=CLEAN_ROOMS_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_CLEAN_ZONE, clean_zone, schema=CLEAN_ZONE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAN_OVERDUE, clean_overdue, schema=CLEAN_OVERDUE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_MAP, refresh_map, schema=REFRESH_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_COMMAND, send_command, schema=SEND_COMMAND_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    return True


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
