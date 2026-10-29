"""Memoria persistente: stanze dell'ultima mappa e data di ultima pulizia per NOME.

Lo storico è legato al nome della stanza e non al numero: se si rifà la
mappatura e la cucina passa dal numero 4 al 2, la sua data di ultima pulizia
resta valida.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

STORAGE_VERSION = 1


def _key(name: str) -> str:
    return name.strip().casefold()


class RoomHistory:
    """Stanze note e ultima pulizia di ciascuna."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self.rooms: dict[int, str] = {}  # ultima tabella numero -> nome vista
        self._last: dict[str, tuple[str, str]] = {}  # chiave -> (nome come scritto, iso)
        self.last_away: datetime | None = None
        self.last_session: dict[str, Any] | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.rooms = {int(k): v for k, v in (data.get("rooms") or {}).items()}
        self._last = {k: (v[0], v[1]) for k, v in (data.get("last_cleaned") or {}).items()}
        self.last_session = data.get("last_session")
        if data.get("last_away"):
            self.last_away = dt_util.parse_datetime(data["last_away"])

    def _data(self) -> dict[str, Any]:
        return {
            "rooms": {str(k): v for k, v in self.rooms.items()},
            "last_cleaned": {k: list(v) for k, v in self._last.items()},
            "last_away": self.last_away.isoformat() if self.last_away else None,
            "last_session": self.last_session,
        }

    def save(self) -> None:
        self._store.async_delay_save(self._data, 5)

    # --- ultima pulizia --------------------------------------------------
    def mark_cleaned(self, names: list[str], when: datetime | None = None) -> None:
        stamp = (when or dt_util.utcnow()).isoformat()
        for name in names:
            self._last[_key(name)] = (name, stamp)
        self.save()

    def last_cleaned(self, name: str) -> datetime | None:
        item = self._last.get(_key(name))
        return dt_util.parse_datetime(item[1]) if item else None

    def overdue(self, names: list[str], days: float, now: datetime | None = None) -> list[tuple[str, datetime | None]]:
        """Stanze non pulite da almeno 'days' giorni, dalla più trascurata."""
        now = now or dt_util.utcnow()
        result: list[tuple[str, datetime | None]] = []
        for name in names:
            last = self.last_cleaned(name)
            if last is None or (now - last).total_seconds() >= days * 86400:
                result.append((name, last))
        # mai pulite per prime, poi dalla più vecchia
        result.sort(key=lambda item: item[1] or datetime.min.replace(tzinfo=now.tzinfo))
        return result

    # --- confronto tra mappe ---------------------------------------------
    def compare(self, new_rooms: dict[int, str]) -> dict[str, Any] | None:
        """Differenze rispetto all'ultima mappa vista (None se uguale o prima lettura)."""
        if not self.rooms or self.rooms == new_rooms:
            return None
        new_names = {_key(n) for n in new_rooms.values()}
        return {
            "prima": {str(k): v for k, v in sorted(self.rooms.items())},
            "dopo": {str(k): v for k, v in sorted(new_rooms.items())},
            "nomi_spariti": [n for n in self.rooms.values() if _key(n) not in new_names],
            "nomi_nuovi": [n for n in new_rooms.values() if _key(n) not in {_key(x) for x in self.rooms.values()}],
        }

    def remember_rooms(self, rooms: dict[int, str]) -> None:
        if rooms != self.rooms:
            self.rooms = dict(rooms)
            self.save()
