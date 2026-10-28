"""Lettura della mappa Dreame per ricavare stanze e nomi.

La mappa arriva come testo base64 (variante URL) compresso con zlib.
Una volta decompressa è fatta così (little-endian):

  intestazione 27 byte
    map_id  int16 | frame_id int16 | tipo frame 1 byte ('I' completo, 'P' parziale)
    robot   x, y, angolo  (3 x int16)
    base    x, y, angolo  (3 x int16)
    dimensione cella int16 | larghezza int16 | altezza int16 | sinistra int16 | alto int16
  pixel     larghezza x altezza byte (6 bit bassi = numero stanza)
  JSON      resto del file: 'seg_inf' contiene le stanze, 'rism' la mappa salvata
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

from .const import ROOM_TYPES

_LOGGER = logging.getLogger(__name__)

HEADER = struct.Struct("<hhB3h3h5h")


class MapDecodeError(Exception):
    """La mappa non è leggibile."""


@dataclass
class Room:
    room_id: int
    name: str
    room_type: int | None = None
    custom_name: bool = False


@dataclass
class MapData:
    map_id: int
    frame_type: str
    robot: tuple[int, int, int]
    charger: tuple[int, int, int]
    grid: int
    width: int
    height: int
    left: int
    top: int
    pixels: bytes
    info: dict[str, Any]
    rooms: dict[int, Room] = field(default_factory=dict)

    def room_at(self, x: int, y: int) -> int | None:
        """Numero della stanza che contiene il punto (in mm)."""
        if not self.grid or not self.pixels:
            return None
        col = (x - self.left) // self.grid
        row = (y - self.top) // self.grid
        if not (0 <= col < self.width and 0 <= row < self.height):
            return None
        room = self.pixels[row * self.width + col] & 0x3F
        return room if room in self.rooms else None

    @property
    def robot_room(self) -> int | None:
        return self.room_at(self.robot[0], self.robot[1])


def _b64(text: str | bytes) -> bytes:
    if isinstance(text, bytes):
        text = text.decode("ascii", "ignore")
    text = text.strip().replace("_", "/").replace("-", "+")
    text += "=" * (-len(text) % 4)
    return base64.b64decode(text)


def _decode_name(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        name = _b64(value).decode("utf-8").strip()
        if name and name.isprintable():
            return name
    except (ValueError, UnicodeDecodeError):
        pass
    return value.strip() or None


def parse_rooms(info: dict[str, Any]) -> dict[int, Room]:
    """Ricava le stanze dal campo 'seg_inf'."""
    rooms: dict[int, Room] = {}
    seg_inf = info.get("seg_inf")
    if isinstance(seg_inf, str):
        try:
            seg_inf = json.loads(seg_inf)
        except ValueError:
            seg_inf = None
    if not isinstance(seg_inf, dict):
        return rooms
    for key, seg in seg_inf.items():
        try:
            room_id = int(key)
        except (TypeError, ValueError):
            continue
        seg = seg if isinstance(seg, dict) else {}
        room_type = seg.get("type")
        custom = _decode_name(seg.get("name"))
        if custom:
            name = custom
        elif isinstance(room_type, int) and room_type in ROOM_TYPES and room_type != 0:
            name = ROOM_TYPES[room_type]
        else:
            name = f"Stanza {room_id}"
        rooms[room_id] = Room(room_id, name, room_type, bool(custom))
    # nomi uguali (due "Camera da letto"): aggiungo il numero
    seen: dict[str, int] = {}
    for room in rooms.values():
        seen[room.name] = seen.get(room.name, 0) + 1
    for room in rooms.values():
        if seen[room.name] > 1 and not room.custom_name:
            room.name = f"{room.name} {room.room_id}"
    return rooms


def decode_raw(raw: bytes) -> MapData:
    """Decodifica il contenuto già decompresso."""
    if len(raw) < HEADER.size:
        raise MapDecodeError("Mappa troppo corta")
    (map_id, _frame_id, ftype, rx, ry, ra, cx, cy, ca, grid, width, height, left, top) = HEADER.unpack_from(raw)
    start = HEADER.size
    end = start + max(width, 0) * max(height, 0)
    if end > len(raw):
        raise MapDecodeError("Dimensioni della mappa non coerenti")
    pixels = raw[start:end]
    info: dict[str, Any] = {}
    tail = raw[end:].strip(b"\x00")
    if tail:
        try:
            info = json.loads(tail.decode("utf-8", "replace"))
        except ValueError as err:
            _LOGGER.debug("JSON della mappa non leggibile: %s", err)
    data = MapData(
        map_id=map_id,
        frame_type=chr(ftype) if 32 < ftype < 127 else str(ftype),
        robot=(rx, ry, ra),
        charger=(cx, cy, ca),
        grid=grid,
        width=width,
        height=height,
        left=left,
        top=top,
        pixels=pixels,
        info=info,
    )
    data.rooms = parse_rooms(info)
    # la mappa salvata ('rism') contiene le stanze quando il frame attuale no
    if "rism" in info:
        try:
            saved = decode(info["rism"])
            if saved.rooms and not data.rooms:
                data.rooms = saved.rooms
            if saved.rooms and not data.pixels.strip(b"\x00"):
                data.pixels, data.width, data.height = saved.pixels, saved.width, saved.height
                data.left, data.top, data.grid = saved.left, saved.top, saved.grid
        except MapDecodeError as err:
            _LOGGER.debug("Mappa salvata non leggibile: %s", err)
    return data


def decode(payload: str | bytes) -> MapData:
    """Decodifica la mappa così come arriva dal robot o dal cloud."""
    try:
        packed = _b64(payload)
    except ValueError as err:
        raise MapDecodeError("La mappa non è in base64") from err
    try:
        raw = zlib.decompress(packed)
    except zlib.error:
        try:
            raw = zlib.decompress(packed, -zlib.MAX_WBITS)
        except zlib.error as err:
            raise MapDecodeError(
                "Mappa non decomprimibile (probabilmente cifrata)"
            ) from err
    return decode_raw(raw)


def describe(data: MapData) -> dict[str, Any]:
    """Riassunto leggibile (per diagnostica e servizi di debug)."""
    return {
        "map_id": data.map_id,
        "frame": data.frame_type,
        "size": [data.width, data.height],
        "grid_mm": data.grid,
        "robot": list(data.robot),
        "robot_room": data.robot_room,
        "rooms": {r.room_id: {"name": r.name, "type": r.room_type, "custom": r.custom_name} for r in data.rooms.values()},
        "json_keys": sorted(data.info.keys()),
    }
