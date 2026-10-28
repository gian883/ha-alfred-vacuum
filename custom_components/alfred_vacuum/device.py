"""Modello del robot D10 Plus: comandi, lettura stato e stanze.

I comandi passano prima dalla rete locale (miio); se il robot non risponde
e c'è una sessione cloud, si riprova dal cloud Xiaomi.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .cloud import CloudError, XiaomiCloud
from .const import (
    A_AREA_CLEAN,
    A_MAP_REQUEST,
    MOP_LEVELS,
    P_FAN,
    P_MOP,
    PIID_EXTEND_DATA,
    PIID_WORK_MODE,
    POLLED_PROPERTIES,
    WORK_MODE_ROOMS,
)
from .map_decoder import MapData, MapDecodeError, decode
from .miio import MiioClient, MiioDeviceError, MiioError

_LOGGER = logging.getLogger(__name__)


class RobotError(Exception):
    """Comando non riuscito."""


class AlfredRobot:
    """Accesso al robot tramite rete locale e, se disponibile, cloud."""

    def __init__(self, did: str, local: MiioClient | None, cloud: XiaomiCloud | None) -> None:
        self.did = did
        self.local = local
        self.cloud = cloud
        self.last_route = "local"

    # --- instradamento ---------------------------------------------------
    async def _call(self, name: str, *args: Any) -> Any:
        errors: list[str] = []
        if self.local:
            try:
                result = await getattr(self.local, name)(self.did, *args)
                self.last_route = "local"
                return result
            except MiioDeviceError as err:
                raise RobotError(f"Il robot ha rifiutato il comando ({err})") from err
            except MiioError as err:
                errors.append(f"locale: {err}")
        if self.cloud and self.cloud.session:
            try:
                result = await getattr(self.cloud, name)(self.did, *args)
                self.last_route = "cloud"
                return result
            except CloudError as err:
                errors.append(f"cloud: {err}")
        raise RobotError("; ".join(errors) or "Nessun collegamento disponibile")

    async def get_properties(self, props: list[tuple[int, int]] | None = None) -> dict[tuple[int, int], Any]:
        return await self._call("get_properties", props or POLLED_PROPERTIES)

    async def set_property(self, prop: tuple[int, int], value: Any) -> None:
        await self._call("set_property", prop, value)

    async def action(self, action: tuple[int, int], ins: list[dict] | None = None) -> Any:
        return await self._call("action", action, ins)

    # --- pulizia stanze --------------------------------------------------
    async def clean_rooms(
        self,
        rooms: list[int],
        state: dict[tuple[int, int], Any],
        repeats: int = 1,
        fan: int | None = None,
        water: int | None = None,
    ) -> None:
        """Pulisce le stanze nell'ordine dato.

        Ogni stanza è [numero, passate, potenza 0-3, acqua 1-3, ordine].
        """
        if not rooms:
            raise RobotError("Nessuna stanza indicata")
        fan_value = fan if fan is not None else int(state.get(P_FAN, 1) or 1)
        water_value = water if water is not None else int(state.get(P_MOP, 1) or 1)
        if water_value not in MOP_LEVELS:
            water_value = 1
        selects = [
            [int(room), max(1, min(3, int(repeats))), int(fan_value), int(water_value), index + 1]
            for index, room in enumerate(rooms)
        ]
        extend = json.dumps({"selects": selects}, separators=(",", ":"))
        await self.action(
            A_AREA_CLEAN,
            [
                {"piid": PIID_EXTEND_DATA, "value": extend},
                {"piid": PIID_WORK_MODE, "value": WORK_MODE_ROOMS},
            ],
        )

    # --- mappa -----------------------------------------------------------
    async def request_map(self) -> tuple[MapData | None, dict[str, Any]]:
        """Chiede la mappa al robot e la decodifica.

        Restituisce (mappa, dettagli). I dettagli servono per capire,
        in caso di problemi, a che punto si è fermata la lettura.
        """
        details: dict[str, Any] = {}
        out = await self.action(
            A_MAP_REQUEST, [{"piid": 2, "value": json.dumps({"frame_type": "I"}, separators=(",", ":"))}]
        )
        outs = out.get("out", []) if isinstance(out, dict) else (out or [])
        values: dict[int, Any] = {}
        for item in outs:
            if isinstance(item, dict) and "piid" in item:
                values[item["piid"]] = item.get("value")
            elif not isinstance(item, dict):
                values[len(values) + 1] = item
        details["route"] = self.last_route
        details["out_piids"] = sorted(values)
        map_text = values.get(1)
        object_name = values.get(3)
        details["object_name"] = object_name
        details["inline_map_length"] = len(map_text) if isinstance(map_text, str) else 0

        if isinstance(map_text, str) and len(map_text) > 64:
            try:
                return decode(map_text), details
            except MapDecodeError as err:
                details["inline_error"] = str(err)

        if not object_name:
            details["error"] = "Il robot non ha indicato il file della mappa"
            return None, details
        if not (self.cloud and self.cloud.session):
            details["error"] = "Serve l'account Xiaomi per scaricare la mappa"
            return None, details

        name = str(object_name).split(",")[0]
        details["encrypted"] = "," in str(object_name)
        url = await self.cloud.get_file_url(name)
        if not url:
            details["error"] = "Il cloud non ha restituito l'indirizzo della mappa"
            return None, details
        payload = await self.cloud.download(url)
        details["file_length"] = len(payload)
        try:
            return decode(payload), details
        except MapDecodeError as err:
            details["error"] = str(err)
            details["file_head"] = payload[:40].decode("ascii", "replace")
            return None, details
